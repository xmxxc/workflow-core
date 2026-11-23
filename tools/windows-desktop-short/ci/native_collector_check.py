"""Exercise the desktop capture/close path on a real packaged Windows ConPTY with an account-free test worker."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

HERE = Path(__file__).resolve().parent
EXPECTED = '[router] Answer to question 1: D_GO'


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')


def worker():
    from bundled_input import windows_vt_input
    cwd=Path.cwd().resolve()
    home=Path(os.environ['CODEX_HOME'])
    # UUIDv7 embeds the local creation instant, as does the real host's session filename.
    now=datetime.now().astimezone()
    bits=uuid.uuid4().int & ((1<<80)-1)
    session=str(uuid.UUID(int=(int(now.timestamp()*1000)<<80) | (bits&~(15<<76)) | (7<<76)))
    name=f'rollout-{now:%Y-%m-%dT%H-%M-%S}-{session}.jsonl'
    record=home/'sessions'/f'{now:%Y/%m/%d}'/name
    record.parent.mkdir(parents=True)
    def emit(kind,payload):
        with record.open('a',encoding='utf-8') as out:
            out.write(json.dumps(dict(timestamp=stamp(),type=kind,payload=payload))+'\n')
    def msg(text,role='user'):
        emit('response_item',dict(type='message',role=role,content=[
            dict(type='input_text' if role=='user' else 'output_text',text=text)]))
    def event(kind,turn):
        emit('event_msg',dict(type=kind,turn_id=turn))
    emit('session_meta',dict(id=session,source='cli',cli_version='0.164.0',cwd=str(cwd),timestamp=stamp()))
    turn=str(uuid.uuid4())
    event('task_started',turn)
    msg(sys.argv[-1])
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, rollout_path TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?,?)',(session,str(cwd),str(record)))
    def callback(stage):
        p=subprocess.run([sys.executable,str(cwd/'desktop_probe.py'),'--worker',stage],env={
            **os.environ,'CODEX_THREAD_ID':session},capture_output=True,text=True,encoding='utf-8',timeout=15)
        if p.returncode:
            print('Controlled fixture callback exit',stage,p.returncode,flush=True)
            raise RuntimeError('callback: '+stage)
    def render(text):
        print('\x1b[2J\x1b[H'+text+'\n\n› Ask Codex to do anything\n\n  controlled · local worker',flush=True)
    with windows_vt_input() as lines:
        for _ in range(160):
            if (cwd/'assignment.json').is_file():
                break
            time.sleep(.05)
        else:
            raise TimeoutError('fixture assignment')
        callback('ask')
        msg('Controlled fixture waiting for answer.', 'assistant')
        event('task_complete',turn)
        render('Controlled fixture waiting for answer.')
        for line in lines:
            if not line:
                continue
            if not line.startswith('\x1b[200~') or not line.endswith('\x1b[201~'):
                raise ValueError('received a different terminal write')
            received=line[6:-6]
            if received!=EXPECTED:
                raise ValueError('wrong public answer')
            turn=str(uuid.uuid4())
            event('task_started',turn)
            msg(received)
            render('› '+received+'\n\n• Controlled fixture consumed the reply.')
            callback('finish')
            msg('Controlled fixture completed.', 'assistant')
            event('task_complete',turn)


def check(args):
    import desktop_probe
    from launch_short import package
    with tempfile.TemporaryDirectory(prefix='wfc-collector-package-') as pkgtmp:
        pkg=package(args.candidate.resolve(),Path(pkgtmp))
        subprocess.run([sys.executable,str(args.desktop_tools/'test_desktop_evidence.py'),'--package',str(pkg)],
                       check=True,timeout=25)
        root=args.scratch.resolve()
        root.mkdir(parents=True,exist_ok=False)
        worker_home=root/'private-worker-home'
        worker_home.mkdir()
        # This CI worker runs entirely in a fresh local home; keep a caller's SQLite
        # override or outer Codex thread out of its exact-id lookup.
        os.environ.pop('CODEX_SQLITE_HOME',None)
        os.environ.pop('CODEX_THREAD_ID',None)
        args.evidence.mkdir(parents=True,exist_ok=False)
        try:
            trial,result=desktop_probe.run_trial(pkg,[sys.executable,str(Path(__file__).resolve()),args.case],worker_home,root,
                kind='native_collector_fixture',cli_version='0.164.0',
                cli_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        finally:
            # This freshly created CI scratch contains at most one trial. Copy only the
            # driver's shareable files even if the run raised before returning its path.
            import shutil
            observations=list(root.glob('wfc-desktop-short-*/evidence'))
            if len(observations)==1:
                for name in ('result.json','inputs.jsonl'):
                    src=observations[0]/name
                    if src.is_file():
                        shutil.copy2(src,args.evidence/name)
        start = result['startup']
        diff = start['comparison']
        assert start['spawn_audited'] and diff['cmdline_matches'] and diff['cwd_matches']
        assert diff['argv_differences'] == [] and diff['env_differences'] == []
        assert start['prepared']['argv'][-1]['role']=='dispatch-brief'
        cleanup = result['cleanup']
        assert cleanup['confirmed'] and cleanup['signed_receipt'] and cleanup['all_four_exited']
        assert set(cleanup['identities'].values()) == {'exit-verified'}
        assert cleanup['backend_alive'] is False and cleanup['waiters']==0 and cleanup['private_state_removed']
        assert not (trial/'private-state').exists()
        if args.case == 'early':
            assert result['verdict']=='blocked' and result['reply_invocations']==0
            assert not (args.evidence/'inputs.jsonl').exists()
            assert start['receipt']['category']=='process_exited' and start['receipt']['exit_code']==17
            assert 'fixture-startup-sentinel' in start['receipt']['diagnostic']['signals']
            assert 'fixture-startup-sentinel' in start['natural_final_screen']['signals'] and start['signed_end']
            assert 'SENSITIVE_FIXTURE_CONTENT' not in (args.evidence/'result.json').read_text()
        else:
            assert result['verdict']=='consumed' and result['reply_invocations']==1
            assert len(result['closed_rollout']['exact'])==1 and result['writes_confirmed']
            assert result['receipts']['done_ack']['state']=='processed'
        print('PASS Windows Job / ConPTY collector case', args.case)


if __name__=='__main__':
    if sys.argv[1:2]==['early']:
        print('WFC controlled startup sentinel SENSITIVE_FIXTURE_CONTENT',flush=True)
        print('WFC controlled startup sentinel SENSITIVE_FIXTURE_CONTENT',file=sys.stderr,flush=True)
        time.sleep(.25)
        sys.exit(17)
    if sys.argv[1:2]==['worker']:
        worker()
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--case', choices=['worker','early'], required=True)
        for arg in ('candidate','desktop-tools','scratch','evidence'):
            parser.add_argument('--'+arg,required=True,type=Path)
        args=parser.parse_args()
        sys.path.insert(0,str(args.desktop_tools.resolve()))
        check(args)
