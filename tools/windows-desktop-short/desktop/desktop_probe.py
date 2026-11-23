"""Foreground one-chat measurement against the installed Codex and the 2409c40 Windows package."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from uuid import UUID, uuid4
from contextlib import closing

HERE = Path(__file__).resolve().parent
NATIVE_SHA = '6f267830b029ee25d9339864aa867cd45de9f8fd4c5ea007d2e63c13a0c66d36'
ANSWER = 'D_GO'
QUESTION = 'D_GO?'
REPORT = 'WFC desktop reply consumed '


class TrialError(Exception):
    """Only deliberately selected, credential-free messages belong in the evidence."""


class BindingError(TrialError):
    """An observed immutable identity conflicts with this execution."""


class PendingRollout(TrialError):
    pass


def bind(value, reason):
    if not value:
        raise BindingError(reason)


def check(value, reason):
    if not value:
        raise TrialError(reason)


def file_sha(path):
    with path.open('rb') as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
        return h.hexdigest()


def write_json(path, value):
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=True, indent=2), encoding='utf-8')
    tmp.replace(path)


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def clean_error(exc):
    return str(exc) if isinstance(exc, TrialError) else type(exc).__name__


def checked_session(value):
    try:
        normalized = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TrialError('worker shell did not supply a session UUID') from exc
    check(normalized == value, 'worker session id spelling is ambiguous')
    return normalized


def rollout_for_session(home, session, work, *, index_dir=None):
    """Point lookup through the thread id index. Never enumerate a shared session directory."""
    session = checked_session(session)
    db = (index_dir or home) / 'state_5.sqlite'
    if not db.is_file():
        raise PendingRollout('worker state index is unavailable')
    uri = db.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
        connection.execute('PRAGMA query_only=ON')
        stmt = 'SELECT cwd, rollout_path FROM threads WHERE id=?'
        plan = connection.execute('EXPLAIN QUERY PLAN ' + stmt, (session,)).fetchall()
        check(any('SEARCH threads' in r[3] and 'id=?' in r[3] for r in plan)
              and not any('SCAN ' in r[3] for r in plan), 'the worker lookup lacks an exact id index')
        rows = connection.execute(stmt, (session,)).fetchmany(2)
    if not rows:
        raise PendingRollout('worker session is absent from its exact id index')
    check(len(rows) == 1, 'more than one row for the worker id')
    directory, filename = map(Path, rows[0])
    check(directory.is_absolute() and directory.samefile(work), 'indexed worker cwd mismatch')
    check(filename.is_absolute() and filename.name.startswith('rollout-') and
          filename.name.endswith('-' + session + '.jsonl'), 'indexed worker rollout name mismatch')
    check(filename.resolve(strict=True).is_relative_to((home/'sessions').resolve(strict=True)),
          'indexed worker rollout outside the host sessions directory')
    return filename


def worker(stage):
    work = HERE
    deadline = time.monotonic() + 6
    assignment_path = work/'assignment.json'
    while not assignment_path.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    a = load(assignment_path)
    session = checked_session(os.environ.get('CODEX_THREAD_ID'))
    check(a['dispatch'] == os.environ.get('WFC_DISPATCH') and os.environ.get('WFC_WORKER_CALLBACK'),
          'worker callback identity missing or changed')
    check(Path(os.environ['WFC_STATE']).resolve() == Path(a['state']).resolve(), 'wrong worker WFC state')
    with (work/(stage + '.claimed')).open('x'):
        pass
    if stage == 'ask':
        args = ['ask', QUESTION]
    else:
        previous = load(work/'ask.json')
        check((previous['session'],previous['attempt']) == (session,a['attempt']), 'worker session changed')
        (work/'report.md').write_text(REPORT+a['attempt'], encoding='utf-8')
        args = ['done','--report',str(work/'report.md'),'--message-id',f"done:{a['dispatch']}:{a['attempt']}"]
    env = {**os.environ, 'CODEX_HOME':a['router_home'], 'PYTHONUTF8':'1'}
    # WFC callback opens only the private router state. The active Codex keeps its usual profile.
    p = subprocess.run([sys.executable, a['pyz'], *args, '--attempt', a['attempt']], env=env, cwd=work,
                       capture_output=True, text=True, encoding='utf-8', timeout=12)
    check(p.returncode == 0, 'worker WFC callback failed')
    receipt = json.loads(p.stdout)
    write_json(work/(stage+'.json'), dict(session=session, dispatch=a['dispatch'], attempt=a['attempt'],
                                         messageId=receipt['messageId'], id=receipt['message']))
    print('Worker callback recorded.')


def one_reply(plan_file):
    """Open the collector before invoking the same public entry point as the packaged wfc.cmd."""
    from desktop_evidence import input_observation, snapshot
    plan = load(plan_file)
    sys.path.insert(0, plan['pyz'])
    import bundled_runtime
    import router
    import wfc_platform
    call = bundled_runtime.call
    trace_path = Path(plan['trace'])
    expected = f"[router] Answer to question {plan['question']}: {ANSWER}"
    # Exclusive creation also fences accidental reentry of this diagnostic subprocess.
    with trace_path.open('x', encoding='utf-8', buffering=1) as trace:
        def append(ev):
            trace.write(json.dumps(ev)+'\n')
            trace.flush()
        append({'op':'collector_ready','at':time.monotonic()})
        input_count = 0
        ready = False
        def observe(handle, op, **data):
            nonlocal input_count, ready
            bound = (handle.get('unit'),handle.get('attempt'),handle.get('generation'))
            check(bound == (plan['dispatch'],plan['attempt'],plan['generation']), 'reply execution binding changed')
            ev = dict(op=op,start=time.monotonic())
            if op == 'input':
                ev.update(input_observation(data['text'],expected))
                if input_count == 0:
                    worker_id = plan['worker']
                    if (not ready or not ev.get('exact') or
                            wfc_platform.process_identity(worker_id['pid']) != worker_id['creation']):
                        append({**ev,'phase':'blocked-before-input'})
                        raise TrialError('live idle composer or first paste changed before submission')
                elif input_count != 1 or ev['kind'] != 'enter':
                    append({**ev,'phase':'blocked-before-input'})
                    raise TrialError('additional input suppressed after single paste and submit')
                input_count += 1
                # Distinguish a dispatched write whose response is lost from an unrequested write.
                append({**ev,'phase':'request'})
            result = call(handle,op,**data)
            ev.update(end=time.monotonic(),result=result['result'],phase='response')
            if op == 'read':
                ev['screen'] = snapshot(result.get('data',{}),router,expected)
                ready = result['result']=='accepted' and ev['screen']['ready_to_reply']
            append(ev)
            return result
        bundled_runtime.call = observe
        sys.argv = [plan['pyz'],'reply',str(plan['question']),ANSWER]
        runpy.run_path(plan['pyz'],run_name='__main__')


def run_trial(package, host_command, worker_home, trial_parent, *, kind, cli_version, cli_sha256,
              worker_term=None, model=None, effort=None, worker_scratch=False, candidate=None, pyz_sha=None,
              copy_callback_program=False):
    import desktop_evidence as evidence
    import startup
    from desktop_evidence import EvidenceError
    from launch_short import package_binding
    check(kind in ('desktop','native_collector_fixture'), 'unknown run kind')
    package = Path(package).resolve(strict=True)
    worker_home = Path(worker_home).resolve(strict=True)
    parent = Path(trial_parent).resolve(strict=True)
    identity, payload = package_binding(candidate, pyz_sha)
    for name,digest in payload.items():
        check(file_sha(package/name) == digest, 'candidate file hash mismatch: '+name)
    check((package/'COMMIT.txt').read_text().strip() == identity, 'candidate commit mismatch')
    if kind == 'desktop':
        check(cli_version=='codex-cli 0.164.0' and cli_sha256==NATIVE_SHA, 'independent Codex identity mismatch')
    pyz = package/'wfc.pyz'
    sys.path.insert(0,str(pyz))
    import bundled_runtime
    import router
    import wfc_platform
    import host_launch
    if worker_scratch:
        # Match the Windows takeover fixture's per-user scratch workspace.
        # Python 3.13 mkdtemp gives the sandbox a different DACL from a normal workspace.
        trial = Path(tempfile.gettempdir()).resolve() / ('wfc-desktop-short-' + uuid4().hex)
        trial.mkdir(exist_ok=False)
    else:
        trial = Path(tempfile.mkdtemp(prefix='wfc-desktop-short-',dir=parent)).resolve()
    (trial/'.gitignore').write_text('*\n',encoding='ascii')
    print('Trial '+str(trial),flush=True)
    work, out, state, router_home = (trial/n for n in ('work','evidence','private-state','router-home'))
    for p in (work,state):
        p.mkdir()
    for p in (out,router_home):
        p.mkdir(mode=0o700)
    # Remove inherited WFC capabilities, route selectors and caller thread identity.
    env = {k:v for k,v in os.environ.items() if not k.upper().startswith('WFC_') and k.upper()!='CODEX_THREAD_ID'}
    if worker_scratch:
        for key in ('CODEX_WINDOWS_REGISTERED_CORE','CODEX_APP_RUNTIME_CLI_PATH',
                    'CODEX_APP_TOOLS_PIPE_PATH','CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY'):
            env.pop(key, None)
    env.update(WFC_STATE=str(state),CODEX_HOME=str(router_home),PYTHONUTF8='1',
               PATH=str(Path(sys.executable).parent)+os.pathsep+env.get('PATH',''))
    parent_thread = os.environ.get('CODEX_THREAD_ID')
    host_env = {'CODEX_HOME': str(worker_home)}
    if worker_term is not None:
        host_env['TERM'] = worker_term
    scope = trial.name
    result = dict(kind=kind, candidate=identity, package_sha256=payload, cli_version=cli_version,
                  cli_sha256=cli_sha256, os=platform.platform(), python=sys.version.split()[0],
                  verdict='blocked', cwd={'role':'trial/work','sha256':startup.sha(str(work)),
                                        'parent_is_invocation_cwd':trial.parent == Path.cwd().resolve()},
                  launch_inputs={'model_flag':bool(model), 'effort_flag':bool(effort), 'explicit_runtime':False,
                      'worker_home_source':('isolated-fixture' if kind!='desktop' else
                                            'CODEX_HOME' if os.environ.get('CODEX_HOME') else 'default-home'),
                      'worker_home_sha256':startup.sha(str(worker_home)),
                      'config_files_present':{'worker_home':(worker_home/'config.toml').is_file(),
                          'invocation_project':(parent/'.codex'/'config.toml').is_file(),
                          'worker_project':(work/'.codex'/'config.toml').is_file()},
                      'inherited':startup.env_view(os.environ), 'router':startup.env_view(env),
                      'host_env_overrides':list(host_env),
                      'term_source':'host override' if worker_term else 'inherited',
                      'model_source':'dispatch --model' if model else 'host configuration/default',
                      'requested_model':model,
                      'effort_source':'dispatch --effort' if effort else 'host configuration/default',
                      'requested_effort':effort},
                  screens={}, tracked={},reply_invocations=0,
                  cleanup={'confirmed':False},receipts={})
    job, handle, bound_file, session, expected = None,None,None,None,None
    trace_file = out/'inputs.jsonl'

    def cli(*args, timeout=14):
        cmd = ([sys.executable,str(HERE/'startup.py'),'dispatch',str(pyz)] if args[0]=='dispatch'
               else [sys.executable,str(pyz)]) + list(map(str,args))
        proc = subprocess.run(cmd,env=env,cwd=work,capture_output=True,encoding='utf-8',errors='replace',timeout=timeout)
        if args[0]=='dispatch' and proc.returncode:
            result['dispatch_error']={'exit_code':proc.returncode,'stderr':startup.words(proc.stderr)}
            host_launch.write_private(trial/'dispatch-local.json', {
                'scope':scope, 'exit_code':proc.returncode, 'diagnostic':startup.local_excerpt(proc.stderr),
            })
        check(proc.returncode == 0, f'public {args[0]} exited {proc.returncode}')
        return json.loads(proc.stdout)

    def public_status():
        view=cli('status','--dispatch',job['dispatch'])
        check(len(view['dispatches'])==1,'dispatch status unreadable')
        s=view['dispatches'][0]
        bind(all(s.get(k)==job[k] for k in ('dispatch','scope','attempt','round')) and
              Path(s['cwd']).samefile(work),'public dispatch identity changed')
        return s

    def record_identity(role, ident):
        if type(ident.get('pid')) is int and ident['pid'] > 0 and ident.get('creation') and isinstance(ident['creation'],str):
            item = {k:ident[k] for k in ('pid','creation')}
            bind(result['tracked'].get(role,item)==item,'worker or backend process identity changed')
            result['tracked'][role]=item

    def collect_processes(s):
        h = s.get('backendHandle') or {}
        for role in ('kernel','service','guardian','worker'):
            ident=h.get(role) or {}
            if role=='worker':
                receipt=s.get('launch') or {}
                ident={'pid':receipt.get('pid'),'creation':receipt.get('processStart')}
            record_identity(role, ident)

    def verified_handle(s):
        collect_processes(s)
        h=s.get('backendHandle') or {}
        check(s.get('backendAlive') and (s.get('launch') or {}).get('category')=='started',
              'worker is not live; inspect launch status')
        check(len(result['tracked'])==4 and h.get('unit')==job['dispatch'] and h.get('attempt')==job['attempt']
              and h.get('generation'),'worker/backend identity incomplete')
        check(all(wfc_platform.process_identity(p['pid'])==p['creation'] for p in result['tracked'].values()),
              'worker/backend creation identity changed before input')
        snap=bundled_runtime.read_json(h['snapshot'])
        check(bundled_runtime.capability_matches(h,snap) and snap.get('state')=='active'
              and all(snap.get(k)==h.get(k) for k in ('unit','attempt','generation','nonce','service','kernel','guardian')),
              'active snapshot does not match private dispatch')
        return h,snap

    def current_rollout(closed=False):
        return evidence.read_rollout(bound_file, session=session,cwd=work,dispatch=job['dispatch'],
                attempt=job['attempt'],expected=expected,closed=closed,sessions_root=worker_home/'sessions')

    def screen(snap):
        data=bundled_runtime.call(snap,'read')
        return {'at':time.monotonic(),'result':data['result'],**evidence.snapshot(data.get('data',{}),router,expected)}

    def checked_wait_message(kind_):
        batch=cli('wait','--scope',scope,'--timeout','1')
        messages=batch.get('messages',[])
        check(len(messages)==1 and messages[0].get('type')==kind_, 'unexpected '+kind_+' batch')
        return messages[0]

    def save():
        write_json(out/'result.json',result)

    try:
        shutil.copy2(Path(__file__),work/'desktop_probe.py')
        callback_pyz = pyz
        if copy_callback_program:
            # A ZIP is extracted in the controller's private temp directory.
            callback_pyz = Path(shutil.copy2(pyz, work/'wfc.pyz'))
            check(file_sha(callback_pyz) == payload['wfc.pyz'], 'worker program copy differs')
        write_json(work/'launch.json',{'hosts':{'codex':{'command':list(map(str,host_command)),
                                                       'env':host_env}}})
        ask_cmd = "& '"+str(Path(sys.executable)).replace("'","''")+"' .\\desktop_probe.py --worker "
        (work/'task.md').write_text(
            "This disposable one-chat test uses the bundled worker callback. From your assigned cwd, "
            "run the following PowerShell command exactly once to register your own host session, ask D_GO?, "
            "and end this turn. Do not ask through another tool or start a subagent.\n\n"+ask_cmd+"ask\n\n"
            "When the controller answers D_GO, run this command exactly once to create the assigned report, "
            "record done and end the turn.\n\n"+ask_cmd+"finish\n",encoding='utf-8')
        save()
        job=cli('dispatch','--backend','bundled','--host','codex','--scope',scope,'--cwd',work,
                '--task',work/'task.md','--report',work/'report.md','--launch-config',work/'launch.json',
                *(['--model', model] if model else []), *(['--effort', effort] if effort else []), timeout=22)
        result['assignment']={k:job[k] for k in ('dispatch','attempt','round','scope')}
        write_json(work/'assignment.json',{**result['assignment'],'state':str(state),'router_home':str(router_home),
                                           'pyz':str(callback_pyz)})
        s=public_status()
        collect_processes(s)
        result['launch']={k:s['launch'][k] for k in ('pid','processStart','category','exitCode') if k in (s.get('launch') or {})}
        handle,snap=verified_handle(s)
        result['generation']=handle['generation']
        save()
        start=time.monotonic()
        while not (work/'ask.json').is_file():
            check(time.monotonic()-start<50,'timed out awaiting the worker-owned ask')
            check(wfc_platform.process_identity(result['tracked']['worker']['pid'])==result['tracked']['worker']['creation'],
                  'worker exited before the bound ask')
            time.sleep(.25)
        asked=load(work/'ask.json')
        session=checked_session(asked['session'])
        check(session!=parent_thread and all(asked[k]==job[k] for k in ('dispatch','attempt')),
              'worker shell callback is not uniquely bound')
        q=evidence.check_question(checked_wait_message('question'),job)
        check(q['id']==asked['id'] and q['messageId']==asked['messageId'],'ask receipt identity mismatch')
        result['receipts']['question']=q
        expected=f"[router] Answer to question {q['id']}: {ANSWER}"
        result.update(session=session, expected=expected, expected_sha256=evidence.sha(expected))
        index_dir=Path(os.environ['CODEX_SQLITE_HOME']).resolve() if os.environ.get('CODEX_SQLITE_HOME') else worker_home
        deadline=time.monotonic()+15
        last_issue='worker not ready'
        while True:
            try:
                if bound_file is None:
                    bound_file=rollout_for_session(worker_home,session,work,index_dir=index_dir)
                before=current_rollout()
                result['before']=before
                check(not before['exact'] and not before['projected'],'reply already appears before submission')
                pre=screen(snap)
                result['screens']['before_reply']=pre
                if before['turns'] and before['turns'][-1][1]=='task_complete' and pre['ready_to_reply']:
                    break
                last_issue='worker did not reach a completed turn and a demonstrably idle composer'
            except (OSError, sqlite3.Error, EvidenceError, PendingRollout) as exc:
                last_issue=type(exc).__name__
            if time.monotonic()>=deadline:
                raise TrialError(last_issue)
            time.sleep(.2)
        # Recheck the immutable execution/callback identity after all waiting.
        h,snap=verified_handle(public_status())
        check(h['generation']==handle['generation'],'execution generation changed before reply')
        startup_view, observed_roles=startup.preclose(state,job,h,bundled_runtime)
        result['startup_before_reply']=startup_view
        check(startup.matching_launch(startup_view) and
              observed_roles.get('worker')==result['tracked']['worker'],
              'prepared command, actual spawn or worker start evidence did not match')
        if worker_term is not None:
            check(startup_view['spawn']['env']['TERM'].get('value') == worker_term, 'worker TERM mismatch')
        context = (before.get('turn_context') or [{}])[-1]
        for key, requested in (('model',model),('effort',effort)):
            if requested is not None:
                check(context.get(key) == requested, 'worker turn context differs from requested '+key)
        plan=router_home/'reply-plan.json'
        write_json(plan,dict(pyz=str(pyz),question=q['id'],trace=str(trace_file),generation=h['generation'],
                             dispatch=job['dispatch'],attempt=job['attempt'],worker=result['tracked']['worker']))
        result['reply_invocations']=1
        save()
        proc=subprocess.run([sys.executable,str(HERE/'desktop_probe.py'),'--one-reply',str(plan)],env=env,cwd=work,
                            capture_output=True,encoding='utf-8',errors='replace',timeout=18)
        result['reply_exit']=proc.returncode
        check(proc.returncode==0,'public reply returned a failed or unknown outcome')
        result['receipts']['reply']=evidence.check_reply(json.loads(proc.stdout),job,q['id'])
        ack=cli('ack',q['messageId'])
        check(ack['messageId']==q['messageId'] and ack['state']=='processed','question ACK mismatch')
        result['receipts']['question_ack']={k:ack[k] for k in ('messageId','state','statusVersion')}
        result['screens']['after_reply']=screen(snap)
        save()
        deadline=time.monotonic()+32
        while not (work/'finish.json').is_file() and time.monotonic()<deadline:
            time.sleep(.25)
        result['screens']['before_close']=screen(snap)
        check((work/'finish.json').is_file(),'worker done callback missing before observation deadline')
        done=checked_wait_message('done')
        receipt=load(work/'finish.json')
        check(all(done.get(k)==job[k] for k in ('dispatch','scope','round','attempt')) and
              done['state']=='received' and receipt['messageId']==done['messageId'] and receipt['session']==session and
              Path(done['report']).resolve()==work/'report.md','done identity mismatch')
        report_ok=(work/'report.md').read_text(encoding='utf-8')==REPORT+job['attempt']
        check(report_ok,'report contents mismatch')
        result['report_sha256']=file_sha(work/'report.md')
        result['receipts']['done']={k:done[k] for k in ('id','messageId','state','statusVersion','attempt')}
        ack=cli('ack',done['messageId'])
        check(ack['messageId']==done['messageId'] and ack['state']=='processed','done ACK mismatch')
        result['receipts']['done_ack']={k:ack[k] for k in ('messageId','state','statusVersion')}
    except (Exception, KeyboardInterrupt) as exc:
        result['error']=clean_error(exc)
    finally:
        try:
            cleanup=result['cleanup']
            # A second, private identity witness remains available if a status/read fails.
            dbpath=state/'router.db'
            if dbpath.exists():
                try:
                    with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                        rows=db.execute('SELECT id,scope,current_round,current_attempt,cwd,backend_handle '
                                        'FROM dispatch LIMIT 2').fetchall()
                    bind(len(rows)<=1,'unexpected second dispatch in private state')
                    if rows:
                        d,sc,r,a,cwd,raw_handle=rows[0]
                        bind(sc==scope and Path(cwd).samefile(work),'private execution scope or cwd changed')
                        dbview=dict(dispatch=d,scope=sc,round=r,attempt=a)
                        if job:
                            bind(all(dbview[k]==job[k] for k in dbview),'private execution identity changed')
                        else:
                            job=dbview
                        with closing(sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
                            fault=db.execute("SELECT category,diagnostic FROM fault_event WHERE dispatch=? "
                                             "AND operation='create' ORDER BY created DESC LIMIT 1",(d,)).fetchone()
                        if fault:
                            result['create_fault']=startup.receipt_view({'category':fault[0],'diagnostic':fault[1]})
                            local_path = trial/'dispatch-local.json'
                            host_launch.write_private(local_path, {
                                **startup.read(local_path), **dbview, 'create_fault':startup.local_excerpt(fault[1]),
                            })
                        database_handle=json.loads(raw_handle) if raw_handle else {}
                        if handle and database_handle:
                            bind(all(handle[k]==database_handle.get(k) for k in ('unit','attempt','generation')),
                                 'private execution generation changed')
                        if database_handle == {'state':'not-started'} and not handle:
                            cleanup['private_not_started']=True
                        elif database_handle:
                            bind(database_handle.get('unit')==d and database_handle.get('attempt')==a,
                                 'database backend identity mismatch')
                            handle=database_handle
                except BindingError:
                    raise
                except Exception as exc:
                    cleanup['private_lookup_error']=clean_error(exc)
            if not job:
                views=cli('status')['dispatches']
                candidates=[s for s in views if s.get('scope')==scope and Path(s['cwd']).resolve()==work]
                bind(len(views)==len(candidates)<=1,'ambiguous private execution during cleanup')
                if candidates:
                    job=candidates[0]
            if job:
                result.setdefault('assignment',{k:job[k] for k in ('dispatch','attempt','round','scope')})
                try:
                    s=public_status()
                    result['preclose_public_launch']=startup.receipt_view(s.get('launch') or {})
                    latest=s.get('backendHandle') or {}
                    if latest=={'state':'not-started'} and not handle and cleanup.get('private_not_started'):
                        cleanup['public_not_started']=True
                    elif latest:
                        bind(latest.get('unit')==job['dispatch'] and latest.get('attempt')==job['attempt']
                             and (not handle or handle['generation']==latest.get('generation')),
                             'public execution changed before close')
                        handle=latest
                    collect_processes(s)
                except BindingError:
                    raise
                except Exception as exc:
                    cleanup['preclose_observation_error']=clean_error(exc)
                # Retrieve the durable final record even when public status already sees a dead worker.
                if handle and handle.get('snapshot'):
                    try:
                        view, roles = startup.preclose(state,job,handle,bundled_runtime,
                                                  startup_failed=bool(result.get('error')) and not result['reply_invocations'],
                                                  local_path=trial/'startup-local.json')
                        result['startup']=view
                        for role, ident in roles.items():
                            record_identity(role, ident or {})
                    except Exception as exc:
                        cleanup['startup_observation_error']=clean_error(exc)
                save()
                # Package close checks the capability and generation in the private state.
                close=cli('close',job['dispatch'],timeout=24)
                check(close['result']=='completed' and close['dispatch']==job['dispatch'] and
                      close['attempt']==job['attempt'],'close receipt identity mismatch')
                cleanup['close']={k:close[k] for k in ('dispatch','attempt','result')}
                try:
                    public=cli('status','--dispatch',job['dispatch'])
                    final=public['dispatches'][0]
                    bind(all(final[k]==job[k] for k in ('dispatch','scope','round','attempt')),'closed dispatch mismatch')
                    cleanup['backend_alive']=final['backendAlive']
                    cleanup['waiters']=len(public['waiters'])
                except Exception as exc:
                    cleanup['postclose_status_error']=clean_error(exc)
                if handle and handle.get('snapshot'):
                    try:
                        end=bundled_runtime.read_json(handle['snapshot'])
                        cleanup['signed_receipt']=bundled_runtime.confirmed_cleanup(handle,end)
                    except Exception as exc:
                        cleanup['snapshot_error']=clean_error(exc)
                deadline=time.monotonic()+6
                while True:
                    exit_checks={}
                    for role,ident in result['tracked'].items():
                        observed=wfc_platform.process_probe(ident['pid'])
                        exit_checks[role]=(observed['state']=='exited' or observed['state']=='alive' and
                                           observed['identity']!=ident['creation'])
                    if all(exit_checks.values()) or time.monotonic()>=deadline:
                        break
                    time.sleep(.1)
                cleanup['identity_exits']=exit_checks
                cleanup['all_four_exited']=len(exit_checks)==4 and all(exit_checks.values())
                empty_prelaunch=(cleanup.get('private_not_started') and cleanup.get('public_not_started')
                                 and not handle and not exit_checks and not list((state/'bundled').glob('*')))
                cleanup['confirmed']=((cleanup.get('signed_receipt') is True or bool(empty_prelaunch)) and
                    all(exit_checks.values()) and cleanup.get('backend_alive') is False and cleanup.get('waiters')==0)
            else:
                check(not list((state/'bundled').glob('*.json')), 'backend record without a bound dispatch')
                cleanup.update(confirmed=True,dispatches=0,all_four_exited=False)
        except (Exception, KeyboardInterrupt) as exc:
            result['cleanup']['error']=clean_error(exc)
        if result['cleanup']['confirmed'] and bound_file and expected:
            try:
                result['closed_rollout']=current_rollout(closed=True)
            except Exception as exc:
                result['closed_read_error']=clean_error(exc)
        if result['reply_invocations']:
            result['verdict']='failed'
            try:
                events=[json.loads(line) for line in trace_file.read_text(encoding='utf-8').splitlines()]
                sent=[e for e in events if e.get('op')=='input' and e.get('phase')=='request']
                confirmed=[e for e in events if e.get('op')=='input' and e.get('phase')=='response']
                writes_ok=([e['kind'] for e in sent]==['paste','enter'] and
                           [e['kind'] for e in confirmed]==['paste','enter'] and
                           sent[0]['payload_sha256']==result['expected_sha256'] and
                           all(e['result']=='accepted' for e in confirmed))
                result['writes_confirmed']=writes_ok
                if (writes_ok and result['cleanup']['confirmed'] and result['cleanup'].get('all_four_exited') and
                        len(result.get('closed_rollout',{}).get('exact',[]))==1 and
                        'done_ack' in result['receipts'] and 'error' not in result):
                    result['verdict']='consumed'
            except Exception as exc:
                result['trace_read_error']=clean_error(exc)
        cleanup=result['cleanup']
        exits=cleanup.get('identity_exits',{})
        cleanup['identities']={role:('creation-missing' if role not in result['tracked'] else
                                     'exit-verified' if exits.get(role) else 'exit-unverified')
                               for role in startup.ROLES}
        if result['verdict']=='consumed':
            launch=result.get('startup',{})
            if cleanup.get('startup_observation_error') or not launch or not startup.matching_launch(launch):
                result.update(verdict='failed',error='launch observation unavailable or inconsistent')
        if result['cleanup']['confirmed']:
            try:
                shutil.rmtree(state)
                shutil.rmtree(router_home)
                result['cleanup']['private_state_removed']=True
            except OSError as exc:
                result['cleanup']['private_state_removed']=False
                result['cleanup']['remove_error']=clean_error(exc)
        save()
        print('Evidence '+str(out),flush=True)
    return trial,result


if __name__=='__main__':
    if sys.argv[1:2]==['--worker'] and sys.argv[2:] in (['ask'],['finish']):
        worker(sys.argv[2])
    elif sys.argv[1:2]==['--one-reply'] and len(sys.argv)==3:
        one_reply(Path(sys.argv[2]))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--package',required=True,type=Path)
        parser.add_argument('--codex',required=True,type=Path)
        parser.add_argument('--trial-parent',type=Path,default=Path.cwd())
        parser.add_argument('--worker-term', choices=['dumb','xterm-256color'])
        parser.add_argument('--model', choices=['gpt-6.1-sol'])
        parser.add_argument('--effort', choices=['high'])
        parser.add_argument('--copy-callback-program', action='store_true')
        parser.add_argument('--worker-scratch', action='store_true')
        parser.add_argument('--candidate-id')
        parser.add_argument('--pyz-sha256')
        args=parser.parse_args()
        check(os.name=='nt' and sys.version_info>=(3,9),'native Windows with Python 3.9+ is required')
        exe=args.codex.resolve(strict=True)
        digest=file_sha(exe)
        check(digest==NATIVE_SHA,'expected the verified native independent Codex 0.164.0 x64 binary')
        raw=subprocess.check_output([str(exe),'--version'],text=True,timeout=8).strip()
        check(raw=='codex-cli 0.164.0','native CLI version differs')
        home=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
        _,result=run_trial(args.package,[str(exe),'--no-daemon'],home,args.trial_parent,
                           kind='desktop',cli_version=raw,cli_sha256=digest,worker_term=args.worker_term,
                           model=args.model,effort=args.effort,worker_scratch=args.worker_scratch,
                           copy_callback_program=args.copy_callback_program,
                           candidate=args.candidate_id,pyz_sha=args.pyz_sha256)
        sys.exit(0 if result['verdict']=='consumed' else 1)
