"""One native Windows takeover/reply in a fresh state. Upload only the output directory."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from local_provider import Provider, user
from reply_observer import bottom, EXPECTED, safe_text, sha

HERE = Path(__file__).resolve().parent
COMMIT = '2409c40b469e0e40bd5c55326df3d5ebb86ca81b'
PYZ_SHA = 'ba60b239aeb48b6014cf4f7b4925ce2897db4c8d0ad3e131c60603a0ab87d914'
KERNEL_SHA = 'ac383ae828e21dc3fa33d306d895ccf188917fccdac3ef2c10b642f3271cfa15'


def filehash(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding='utf-8')


def require(ok, msg):
    if not ok:
        raise RuntimeError(msg)


def rollout(home, job, work):
    reports = []
    for path in (home/'sessions').rglob('rollout-*.jsonl'):
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 32_000_000, 'rollout bounds')
        raw = path.read_bytes()
        rows = [json.loads(l) for l in raw.splitlines() if l.strip()]
        if not rows or rows[0]['type'] != 'session_meta':
            continue
        m = rows[0]['payload']
        if (Path(m['cwd']) != work or not path.name.endswith('-'+m['id']+'.jsonl') or
                isinstance(m.get('source'), dict) and 'subagent' in m['source']):
            continue
        brief, users, exact, projected, turns, output = [], [], [], [], [], []
        for n,row in enumerate(rows,1):
            p = row['payload']
            if row['type'] == 'response_item':
                text = user(p)
                if text is not None:
                    if (text.startswith('You are a worker dispatched by the controller (dispatch '+job['dispatch']+').')
                            and '--attempt '+job['attempt'] in text):
                        brief.append(n)
                    users.append(dict(line=n,sha256=sha(text),bare=text=='D_GO',contains_marker='D_GO' in text))
                    if text == EXPECTED:
                        exact.append(n)
                if p.get('role') == 'assistant':
                    for part in p.get('content',[]):
                        if part.get('text') in ('WFC_LOCAL_ASK_TURN_END','WFC_LOCAL_REPLY_TURN_END'):
                            output.append([n,part['text']])
            if row['type'] == 'event_msg':
                if p['type']=='user_message' and p.get('message') == EXPECTED:
                    projected.append(n)
                if p['type'] in ('task_started','task_complete','turn_aborted','error'):
                    turns.append([n,p['type'],p.get('turn_id'),row.get('timestamp')])
        if brief:
            reports.append(dict(file=path.name,sha256=hashlib.sha256(raw).hexdigest(),session=m['id'],
                                version=m.get('cli_version'),source=m.get('source'),brief=brief,user_rows=users,
                                exact=exact,projected=projected,turns=turns,assistant_markers=output))
    return reports


def settings_probe(codex, env, work):
    """Ask the pinned app server about this scratch project's layer and Windows sandbox. No thread."""
    import queue
    received = queue.Queue()
    command = [str(codex), 'app-server', '--listen', 'stdio://']
    server = subprocess.Popen(command, env=env, cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, encoding='utf-8', errors='replace')
    def drain():
        try:
            for line in server.stdout:
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if 'id' in reply:
                    received.put(reply)
        finally:
            received.put(None)
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    def send(message):
        server.stdin.write(json.dumps(message)+'\n')
        server.stdin.flush()
    def query(seq, method, params=None):
        req = {'id':seq, 'method':method}
        if params is not None:
            req['params'] = params
        send(req)
        end = time.monotonic() + 10
        while True:
            reply = received.get(timeout=max(.01, end-time.monotonic()))
            if reply is None:
                raise ValueError('app-server EOF')
            if reply.get('id') != seq:
                if time.monotonic() >= end:
                    raise TimeoutError(method)
                continue
            if 'error' in reply:
                return None, dict(code=reply['error'].get('code'), message=safe_text(reply['error'].get('message',''))[:180])
            return reply['result'], None
    result = {}
    try:
        _, error = query(1,'initialize',{'clientInfo':{'name':'wfc_probe','title':'WFC probe','version':'1'},
                                          'capabilities':{'experimentalApi':True}})
        if error:
            result['initialize_error'] = error
        else:
            send({'method':'initialized'})
            view, error = query(2,'config/read',{'cwd':str(work),'includeLayers':True})
            if error:
                result['config_read_error'] = error
            else:
                cfg = view['config']
                result['configured_windows_sandbox'] = (cfg.get('windows') or {}).get('sandbox')
                result['effective_project_marker'] = (cfg.get('tui') or {}).get('animations') is False
                result['project_layers'] = []
                for layer in view.get('layers') or []:
                    src = layer['name']
                    if src.get('type') == 'project' and Path(src['dotCodexFolder']).resolve() == (work/'.codex').resolve():
                        result['project_layers'].append(dict(disabled=bool(layer.get('disabledReason')),
                            disabled_reason=safe_text(layer.get('disabledReason') or '')[:240],
                            contains_marker=(layer.get('config',{}).get('tui') or {}).get('animations') is False))
            ready, error = query(3,'windowsSandbox/readiness')
            if error:
                result['readiness_error'] = error
            else:
                result['readiness'] = ready.get('status')
            requirements, error = query(4,'configRequirements/read')
            if error:
                result['requirements_error'] = error
            else:
                result['allowed_implementations'] = (requirements.get('requirements') or {}).get('allowedWindowsSandboxImplementations')
    except (OSError, TimeoutError, ValueError, KeyError, queue.Empty) as e:
        result['rpc_failure'] = type(e).__name__
    finally:
        try:
            server.stdin.close()
        except OSError:
            pass
        try:
            server.wait(timeout=4)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=4)
        reader.join(2)
        server.stdout.close()
        result['app_server_exit'] = server.returncode
    return result


def tracked_identities(handle, launch, captured):
    tracked = []
    for role in ('guardian','kernel','service'):
        p = handle.get(role) or {}
        if p.get('pid') and p.get('creation'):
            tracked.append(dict(role=role,pid=p['pid'],creation=p['creation']))
    host = launch if launch.get('pid') and launch.get('processStart') else captured
    if host.get('pid') and host.get('processStart'):
        tracked.append(dict(role='host',pid=host['pid'],creation=host['processStart']))
    return tracked


def main(args):
    require(os.name=='nt' and sys.version_info>=(3,11), 'requires native Windows and Python 3.11+')
    pkg, codex, out = (p.resolve() for p in (args.package,args.codex,args.out))
    pyz = pkg/'wfc.pyz'
    require(filehash(pyz)==PYZ_SHA and filehash(pkg/'wfc-kernel.exe')==KERNEL_SHA, 'candidate package mismatch')
    require(filehash(codex)=='6f267830b029ee25d9339864aa867cd45de9f8fd4c5ea007d2e63c13a0c66d36','native digest')
    require((pkg/'COMMIT.txt').read_text().strip()==COMMIT, 'candidate commit mismatch')
    manifest = json.loads((pkg/'BUNDLED-MANIFEST.json').read_text())
    require(filehash(pkg/'wfc.cmd')==manifest['files']['wfc.cmd'], 'candidate entry mismatch')
    out.mkdir(parents=True,exist_ok=False)
    sys.path.insert(0,str(pyz))
    import bundled_runtime
    import router
    import wfc_platform
    result = dict(os=platform.platform(),python=sys.version.split()[0],mode=args.mode,commit=COMMIT,pyz_sha256=PYZ_SHA,
                  kernel_sha256=KERNEL_SHA,codex_sha256=filehash(codex),args_sha256=sha('D_GO'),
                  expected=EXPECTED,expected_sha256=sha(EXPECTED),reply_invocations=0,screens={},tracked=[])
    with tempfile.TemporaryDirectory(prefix='wfc-reply-') as tmp:
        alias_root = Path(tmp)
        root = alias_root.resolve(strict=True)
        require(os.path.samefile(alias_root,root), "temp path file identity")
        work, home, state = root/'work',root/'codex',root/'state'
        for p in (work,home,state,root/'profile',root/'roaming',root/'local',root/'tmp'):
            p.mkdir()
        env = {k:os.environ[k] for k in ('SystemRoot','ComSpec','WINDIR','PATHEXT','ProgramFiles','ProgramFiles(x86)',
                   'ProgramW6432','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS') if k in os.environ}
        env.update(PATH=str(Path(sys.executable).parent)+';'+os.environ['PATH'],HOME=str(root/'profile'),
                   USERPROFILE=str(root/'profile'),APPDATA=str(root/'roaming'),LOCALAPPDATA=str(root/'local'),
                   TMP=str(root/'tmp'),TEMP=str(root/'tmp'),CODEX_HOME=str(home),WFC_STATE=str(state),TERM='xterm-256color',
                   PYTHONUTF8='1',WFC_BUNDLED_DIAGNOSTICS='1',PROBE_PYZ=str(pyz),PROBE_WORK=str(work))
        hooks=root/'hooks'
        hooks.mkdir()
        shutil.copy2(HERE/'reply_observer.py',hooks/'reply_observer.py')
        (hooks/'sitecustomize.py').write_text(
            "import os, sys\n"
            "if sys.argv[1:2] == ['reply'] and 'PROBE_TRACE' in os.environ:\n"
            "    from reply_observer import install\n    install()\n"
            "elif sys.argv[1:2] in (['dispatch'], ['_host-run']) and 'PROBE_STARTUP' in os.environ:\n"
            "    from reply_observer import capture_launch\n    capture_launch()\n", encoding='ascii')
        health={}
        result['preflight']=health
        def cli(*params, extra=None, timeout=28):
            proc=subprocess.run([str(pkg/'wfc.cmd'), *map(str,params)], env={**env,**(extra or {})}, cwd=work,
                                capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
            if proc.returncode:
                # Owner results contain credentials: record only bounded failure stderr after redaction.
                import host_launch
                raise RuntimeError(params[0]+' exit '+str(proc.returncode)+' '+host_launch.sanitize(proc.stderr,env)[-600:])
            return json.loads(proc.stdout)
        provider = Provider(('127.0.0.1',0))
        thread = threading.Thread(target=provider.serve_forever)
        thread.start()
        job = None
        def until(fn, seconds, label):
            end=time.monotonic()+seconds
            while True:
                if provider.error:
                    raise RuntimeError(provider.error)
                x=fn()
                if x:
                    return x
                if time.monotonic()>=end:
                    raise TimeoutError(label)
                time.sleep(.15)
        def status():
            return cli('status','--dispatch',job['dispatch'])['dispatches'][0]
        def ident(s):
            return [s['dispatch'],s['round'],s['attempt'],s['backendHandle']['generation'],
                    s['launch'].get('pid'),s['launch'].get('processStart')]
        def capture(label):
            v=bundled_runtime.call(snapshot,'read')
            result['screens'][label]=dict(at=time.monotonic(),result=v['result'],**bottom(v.get('data',{}),router))
        def startup_state():
            # Direct reads of this isolated generation's files survive closure of its RPC endpoint.
            receipt_path=Path(handle['receipt'])
            snap_path=Path(handle['snapshot'])
            receipt=bundled_runtime.read_json(receipt_path)
            snap=bundled_runtime.read_json(snap_path)
            record=dict(at=time.monotonic(),launch={k:safe_text(receipt[k]) if k=='diagnostic' else receipt[k]
                for k in ('category','diagnostic','exitCode','pid','processStart','at') if k in receipt},
                state=snap.get('state'),cleanup=snap.get('cleanup'))
            if snap.get('final_screen'):
                record['final_viewport']=safe_text(snap['final_screen'])[:24000]
            return record
        def initial_ready():
            if provider.opened.is_set():
                return True
            if time.monotonic()>=initial_ready.next_at:
                initial_ready.next_at=time.monotonic()+.5
                record=startup_state()
                events=result.setdefault('startup_observations',[])
                if record['state']=='active':
                    reply=bundled_runtime.call(snapshot,'read')
                    record['read']=reply['result']
                    record['viewport']=safe_text(reply.get('data',{}).get('text',''))[:24000]
                    record['diagnostic']=safe_text(reply.get('diagnostic',''))
                signature={k:v for k,v in record.items() if k!='at'}
                if signature!=initial_ready.previous and len(events)<40:
                    events.append(record)
                    initial_ready.previous=signature
                if record['state'] in ('exited','faulted','closed'):
                    raise RuntimeError('host ended during startup; see startup_observations and preclose')
                if record['launch'].get('exitCode') is not None:
                    # The supervisor writes exitCode just before the service publishes final_screen.
                    if initial_ready.exit_seen is None:
                        initial_ready.exit_seen=time.monotonic()
                    elif time.monotonic()-initial_ready.exit_seen>3:
                        raise RuntimeError('host exit receipt without terminal cleanup snapshot yet')
            return False
        initial_ready.exit_seen=None
        initial_ready.previous=None
        initial_ready.next_at=0
        try:
            result['codex_version']=subprocess.check_output([str(codex),'--version'],cwd=work,env=env,
                                                             text=True,timeout=12).strip()
            require(result['codex_version']=='codex-cli 0.164.0', 'native version mismatch')
            helper=HERE/'worker_callback.py'
            (home/'config.toml').write_text(
                'model_provider = "controlled"\nmodel = "controlled"\ncli_auth_credentials_store = "file"\n'
                '[model_providers.controlled]\nname = "Loopback"\n'
                f'base_url = "http://127.0.0.1:{provider.server_port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n'
                '[shell_environment_policy]\ninherit = "all"\n'
                '[mcp_servers.wfcprobe]\ncommand = '+json.dumps(sys.executable)+'\nargs = ['+json.dumps(str(helper))+']\n'
                'env_vars = ["WFC_STATE","WFC_DISPATCH","WFC_WORKER_CALLBACK","PROBE_WORK","PROBE_PYZ","PYTHONUTF8"]\n'
                f'[projects.{json.dumps(str(work))}]\ntrust_level = "trusted"\n',encoding='utf-8')
            # Compare two spelling-only variants in the same synthetic directory. Both
            # config reads use the canonical worker cwd and the same provider settings.
            cfgfile = home/'config.toml'
            canonical_config = cfgfile.read_text(encoding='utf-8')
            alias_work = alias_root/'work'
            require(os.path.samefile(alias_work,work),'workspace file identity')
            health['path_identity'] = dict(alias=str(alias_work),canonical=str(work),samefile=True)
            probe_folder = work/'.codex'
            probe_folder.mkdir()
            marker = probe_folder/'config.toml'
            marker.write_text('[tui]\nanimations = false\n', encoding='utf-8')
            observations={}
            health['project_trust_checks']=observations
            try:
                if str(alias_work) != str(work):
                    cfgfile.write_text(canonical_config.replace('[projects.'+json.dumps(str(work))+']',
                        '[projects.'+json.dumps(str(alias_work))+']'), encoding='utf-8')
                    observations['alias_key']=settings_probe(codex,env,work)
                cfgfile.write_text(canonical_config,encoding='utf-8')
                observations['canonical_key']=settings_probe(codex,env,work)
            finally:
                cfgfile.write_text(canonical_config,encoding='utf-8')
                marker.unlink()
                probe_folder.rmdir()
            # Exercise the same child cwd and profile through read-only commands. No conversation is opened.
            probe=subprocess.run([codex,'mcp','get','wfcprobe','--json'],cwd=work,env=env,
                capture_output=True,encoding='utf-8',errors='replace',timeout=15)
            health['mcp_get_exit']=probe.returncode
            health['mcp_get_stderr']=safe_text(probe.stderr)[:1200]
            if probe.returncode==0:
                entry=json.loads(probe.stdout)
                transport=entry.get('transport') or {}
                health['fixture_config_read']=entry.get('name')=='wfcprobe' and transport.get('args')==[str(helper)]
            from urllib.request import ProxyHandler, build_opener
            # No inherited proxy for the loopback health observation.
            with build_opener(ProxyHandler({})).open(
                    f'http://127.0.0.1:{provider.server_port}/wfc-probe-health',timeout=3) as check:
                health['stub_http']=check.status
            health['config_sha256']=filehash(home/'config.toml')
            health['dirs_exist']=all(Path(env[k]).is_dir() for k in
                ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','CODEX_HOME','WFC_STATE','TMP','TEMP'))
            health['auth_file_present']=(home/'auth.json').exists()
            require(health['stub_http']==204 and health['dirs_exist'] and not health['auth_file_present'],'isolation preflight')
            (work/'task.md').write_text('Use the controlled callback tool to ask D_GO? and end your turn. '
                                       'On reply, use the same tool to finish.\n',encoding='utf-8')
            save(work/'launch.json',{'hosts':{'codex':{'command':[str(codex),'--no-daemon']}}})
            a=cli('owner','acquire','--scope','probe','--owner','A','--lease','300')
            env['WFC_CONTROLLER_TOKEN']=a['token']
            job=cli('dispatch','--backend','bundled','--host','codex','--scope','probe','--model','controlled',
                    '--task',work/'task.md','--cwd',work,'--report',work/'report.md','--launch-config',work/'launch.json',
                    extra=dict(PYTHONPATH=str(hooks),PROBE_STARTUP=str(out)))
            result['assignment']={k:job[k] for k in ('scope','dispatch','round','attempt')}
            save(work/'assignment.json',result['assignment'])
            s=status()
            handle=s['backendHandle']
            snapshot=bundled_runtime.read_json(handle['snapshot'])
            started_file=out/'host-started.json'
            started=json.loads(started_file.read_text()) if started_file.is_file() else {}
            launch=s.get('launch') or {}
            result['tracked']=tracked_identities(handle,launch,started)
            result['missing_process_identities']=sorted({'guardian','kernel','service','host'}-
                                                         {p['role'] for p in result['tracked']})
            if launch.get('category')=='process_exited':
                raise RuntimeError('native host exited before takeover; exit code '+str(launch.get('exitCode')))
            require(len(result['tracked'])==4 and launch.get('pid') and launch.get('processStart'),
                    'incomplete live process identity at startup')
            result['identity_a']=ident(s)
            capture('initial')
            until(initial_ready,45,'initial worker model request')
            b=cli('owner','takeover','--scope','probe','--owner','B','--lease','300')
            env['WFC_CONTROLLER_TOKEN']=b['token']
            result['identity_b']=ident(status())
            require(a['generation']==1 and b['generation']==2 and result['identity_a']==result['identity_b'],'takeover identity')
            provider.allow_ask.set()
            if args.mode=='idle':
                provider.after_ask.set()
            until(lambda:(work/'ask.json').exists(),35,'actual worker ask')
            q,=cli('wait','--scope','probe','--timeout','5')['messages']
            require((q['dispatch'],q['attempt'],q['question'],q['id'],q['messageId'])==
                    (job['dispatch'],job['attempt'],'D_GO?',1,json.loads((work/'ask.json').read_text())['messageId']),
                    'worker question identity')
            result['question']={k:q[k] for k in ('messageId','state','id','statusVersion')}
            until(provider.ask_returned.is_set,25,'model request after actual ask tool output')
            def before():
                try:
                    matches=rollout(home,job,work)
                except json.JSONDecodeError:
                    return None
                if len(matches)!=1 or not matches[0]['turns']:
                    return None
                r=matches[0]
                state=r['turns'][-1][1]
                if ((args.mode=='busy' and state=='task_started') or
                        (args.mode=='idle' and state=='task_complete' and r['assistant_markers'])):
                    return r
            result['before']=until(before,16,'requested reply timing and bound worker rollout')
            require(not result['before']['exact'],'unexpected preexisting reply')
            capture('before_reply')
            result['reply_invocations']+=1
            result['reply_at']=time.monotonic()
            try:
                result['reply']=cli('reply','1','D_GO',extra=dict(PYTHONPATH=str(hooks),PROBE_TRACE=str(out/'pty.jsonl')))
                require(result['reply']==dict(dispatch=job['dispatch'],replied=1),'reply receipt')
                result['ack']=cli('ack',q['messageId'])['state']
                capture('before_end_gate_release')
            finally:
                provider.after_ask.set()
            end=time.monotonic()+25
            while time.monotonic()<end and not provider.error:
                if (work/'finish.json').exists() and any(r['assistant_request']=='finish_returned' for r in provider.records):
                    break
                time.sleep(.2)
            capture('after_reply')
            result['report_ok']=(work/'report.md').is_file() and (work/'report.md').read_text()=='controlled reply consumed '+job['attempt']
            if result['report_ok']:
                d,=cli('wait','--scope','probe','--timeout','5')['messages']
                require(d['type']=='done' and d['dispatch']==job['dispatch'] and d['attempt']==job['attempt'],'done identity')
                result['done_ack']=cli('ack',d['messageId'])['state']
        finally:
            provider.allow_ask.set()
            provider.after_ask.set()
            try:
                if not job:
                    # A timed out/uncertain create can still have allocated an execution in this
                    # fresh state. Recover its public id only for close, never create or reply again.
                    views=cli('status')['dispatches']
                    if len(views)==1:
                        job=views[0]
                if job:
                    if 'snapshot' in locals():
                        try:
                            capture('last_before_close')
                        except Exception as e:
                            result['last_read_error']=type(e).__name__
                    if not result['tracked']:
                        try:
                            launch=status().get('launch') or {}
                            result['partial_launch']={k:launch[k] for k in ('category','exitCode','diagnostic') if k in launch}
                        except Exception:
                            result['partial_launch']={'category':'unavailable'}
                    if 'handle' in locals():
                        try:
                            result['preclose']=startup_state()
                        except (OSError, ValueError, KeyError) as err:
                            result['preclose_read_error']=type(err).__name__
                    result['close']=cli('close',job['dispatch'])['result']
                    view=cli('status','--dispatch',job['dispatch'])
                    result['closed_view']=dict(alive=view['dispatches'][0]['backendAlive'],waiters=len(view['waiters']))
                    end=time.monotonic()+8
                    while True:
                        probes=[wfc_platform.process_probe(p['pid']) for p in result['tracked']]
                        if all(p['state']=='exited' or p['state']=='alive' and p['identity']!=i['creation']
                               for p,i in zip(probes,result['tracked'])) or time.monotonic()>=end:
                            break
                        time.sleep(.1)
                    result['process_checks']=probes
                    result['rollouts']=rollout(home,job,work)
            finally:
                provider.shutdown()
                provider.server_close()
                thread.join(3)
                if 'handle' in locals():
                    path=Path(handle['snapshot'])
                    for role in ('runtime','guardian'):
                        trace=path.with_suffix('.'+role+'.trace.json')
                        if trace.is_file():
                            result.setdefault('lifecycle_trace',{})[role]=bundled_runtime.read_json(trace)['events']
                log=home/'log'/'codex-tui.log'
                if log.is_file():
                    with log.open('rb') as source:
                        source.seek(max(log.stat().st_size-65536,0))
                        lines=source.read(65536).decode('utf-8','replace').splitlines()
                    result['startup_log_issues']=[safe_text(line)[:1000] for line in lines
                        if ('WARN' in line or 'ERROR' in line) and not any(v in line.lower() for v in
                            ('headers','auth.json','instructions','tool arguments','shell snapshot','env_vars'))][-30:]
                if (out/'spawn.json').is_file() and (out/'prepared.json').is_file():
                    result['launch_command_hash_matches']=json.loads((out/'spawn.json').read_text())['windows_cmdline_sha256']==json.loads((out/'prepared.json').read_text())['windows_cmdline_sha256']
                result.update(transport=provider.transport,stub=provider.records,stub_error=provider.error,stub_stopped=not thread.is_alive())
                save(out/'result.json',result)
        row,=result['rollouts']
        require(row['version']=='0.164.0' and row['session']==result['before']['session'] and len(row['exact'])==1,
                'closed worker rollout exact reply count or identity')
        require(not provider.error and sum(r['assistant_request']=='finish' and r['end_user_hash']==sha(EXPECTED)
                                     and r['exact_users']==1 for r in provider.records)==1, 'matching main request')
        writes=[json.loads(l) for l in (out/'pty.jsonl').read_text().splitlines() if json.loads(l)['op']=='input']
        require([e['kind'] for e in writes]==['paste','enter'] and writes[0]['payload_sha256']==sha(EXPECTED)
                and all(e['result']=='accepted' for e in writes),'PTY writes')
        require(result['report_ok'] and result.get('done_ack')=='processed' and result.get('ack')=='processed',
                'worker report or callbacks')
        require(result['close']=='completed' and result['closed_view']==dict(alive=False,waiters=0) and len(probes)==4
                and all(p['state']=='exited' or p['state']=='alive' and p['identity']!=i['creation']
                        for p,i in zip(probes,result['tracked'])) and result['stub_stopped'], 'cleanup')
        print('PASS '+str(out))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--codex',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--mode',choices=['idle','busy'],required=True)
    main(parser.parse_args())
