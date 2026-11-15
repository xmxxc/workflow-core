"""Herdr backend through WFC CLI and an isolated real Herdr service."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parent.parent
BINARY = os.environ.get('WFC_HERDR_TEST_BINARY')
PROGRAM = Path(os.environ.get('WFC_TEST_PROGRAM', ROOT / 'wfc.py')).resolve()


@unittest.skipUnless(BINARY, 'set WFC_HERDR_TEST_BINARY to the pinned Herdr 0.9.5 binary')
class HerdrContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wfch-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.endpoint = 'wfc-' + uuid.uuid4().hex if os.name == 'nt' else str(self.root / 'api.sock')
        config = self.root / 'herdr.toml'
        config.write_text('[terminal]\ndefault_shell = ' + json.dumps('cmd.exe' if os.name == 'nt' else '/bin/sh') + '\n', encoding='utf-8')
        self.env = {**os.environ, 'HERDR_SOCKET_PATH': self.endpoint, 'HERDR_CONFIG_PATH': str(config),
                    'XDG_CONFIG_HOME': str(self.root / 'config'), 'XDG_STATE_HOME': str(self.root / 'herdr-state'),
                    'XDG_DATA_HOME': str(self.root / 'data'), 'XDG_CACHE_HOME': str(self.root / 'cache'),
                    'WFC_STATE': str(self.root / 'wfc-state'), 'CODEX_HOME': str(self.root / 'codex'),
                    'WFC_CLAUDE_CONFIG': str(self.root / 'no-claude.json'), 'WFC_MAX_TURN_SECONDS': '0',
                    'PYTHONUTF8': '1', 'TERM': 'xterm-256color'}
        self.env.pop('WFC_DISPATCH', None)
        self.env.pop('WFC_OWNER_TOKEN', None)
        self.env.pop('WFC_CONTROLLER_TOKEN', None)
        self.env.pop('CODEX_THREAD_ID', None)
        self.env.pop('WFC_OPENCODE_DB', None)
        self.log = (self.root / 'server.log').open('w', encoding='utf-8')
        self.addCleanup(self.log.close)
        self.server = subprocess.Popen([BINARY, 'server'], env=self.env, stdout=self.log, stderr=self.log)
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = subprocess.run([BINARY, 'status', 'server', '--json'], env=self.env, capture_output=True)
            if status.returncode == 0 and b'"running"' in status.stdout:
                break
            if self.server.poll() is not None:
                self.fail('isolated Herdr service exited: ' + (self.root / 'server.log').read_text(encoding='utf-8')[-1000:])
            time.sleep(.05)
        else:
            self.fail('isolated Herdr service did not start')
        self.task = self.root / '任务 with spaces.md'
        self.task.write_text('# controlled task\n', encoding='utf-8')
        self.report = self.root / '报告 with spaces.md'
        self.host = self.root / 'host with spaces.py'
        self.host.write_text('import time\nprint("CONTROLLED_READY",flush=True)\ntime.sleep(60)\n', encoding='utf-8')
        self.launch = self.root / 'launch.json'
        self.launch.write_text(json.dumps({'hosts': {'codex': {'command': [str(self.host)],
                                'runtime': [sys.executable, '-u']}}}), encoding='utf-8')

    def stop_server(self):
        if self.server.poll() is None:
            if os.name == 'nt':
                # This Popen is the isolated test-owned service. Kill its test process tree
                # so crash/restart coverage cannot leave a child holding server.log open.
                subprocess.run(['taskkill', '/PID', str(self.server.pid), '/T', '/F'],
                               capture_output=True, timeout=10, check=True)
            else:
                self.server.terminate()
            self.server.wait(timeout=10)

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(PROGRAM), *args], env=self.env,
                                capture_output=True, text=True, encoding='utf-8', timeout=25)
        self.assertIn(result.returncode, (0, 3), result.stderr)
        return json.loads(result.stdout)

    def dispatch(self, *extra):
        return self.cli('dispatch', '--backend', 'herdr', '--herdr-endpoint', self.endpoint,
                        '--host', 'codex', '--task', str(self.task), '--cwd', str(self.root),
                        '--report', str(self.report), '--launch-config', str(self.launch), *extra)['dispatch']

    def api(self, method, params=None):
        result = subprocess.run([sys.executable, str(PROGRAM), '_herdr-call'],
                                input=json.dumps({'endpoint': self.endpoint, 'method': method, 'params': params or {}}),
                                env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value['result'], 'accepted', value)
        return value['data']

    def test_opencode_initial_prefill_is_submitted_without_retyping(self):
        self.host.write_text(r'''import os,subprocess,sys,textwrap,time
text=sys.argv[sys.argv.index('--prompt')+1]
print('┃',flush=True)
for line in textwrap.wrap(text,64):print('┃  '+line,flush=True)
print('┃\n┃  Build · controlled OpenCode\n╹'+'▀'*70,flush=True)
line=sys.stdin.readline()
if line.strip():raise RuntimeError('initial prompt was typed twice')
subprocess.run([sys.executable,PROGRAM,'ask','INITIAL_PROMPT_SUBMITTED',
                '--attempt',text.split('--attempt ')[1].split()[0]],check=True)
time.sleep(60)
'''.replace('PROGRAM',repr(str(PROGRAM))),encoding='utf-8')
        self.launch.write_text(json.dumps({'hosts':{'opencode':{'command':[str(self.host)],
                                      'runtime':[sys.executable,'-u']}}}),encoding='utf-8')
        dispatch = self.cli('dispatch','--backend','herdr','--herdr-endpoint',self.endpoint,
                            '--host','opencode','--task',str(self.task),'--cwd',str(self.root),
                            '--report',str(self.report),'--launch-config',str(self.launch))['dispatch']
        messages = self.cli('wait','--timeout','1')['messages']
        self.assertTrue(any(m.get('question')=='INITIAL_PROMPT_SUBMITTED' for m in messages),messages)
        self.assertEqual(self.cli('close',dispatch)['result'],'completed')

    def test_initial_prefill_does_not_submit_empty_or_other_drafts(self):
        for index, draft in enumerate(("", "unrelated user draft", "controller")):
            with self.subTest(draft=draft):
                touched = self.root / f"unexpected-enter-{index}"
                self.host.write_text(r'''import sys,threading,time
from pathlib import Path
print('┃  '+DRAFT+'\n┃\n┃  Build · controlled OpenCode\n╹'+'▀'*70,flush=True)
def read():
    line=sys.stdin.readline()
    if line:Path(TOUCHED).write_text(line,encoding='utf-8')
threading.Thread(target=read,daemon=True).start()
time.sleep(60)
'''.replace('DRAFT',repr(draft)).replace('TOUCHED',repr(str(touched))),encoding='utf-8')
                self.launch.write_text(json.dumps({'hosts':{'opencode':{'command':[str(self.host)],
                                              'runtime':[sys.executable,'-u']}}}),encoding='utf-8')
                dispatch=self.cli('dispatch','--backend','herdr','--herdr-endpoint',self.endpoint,
                                  '--host','opencode','--task',str(self.task),'--cwd',str(self.root),
                                  '--report',str(self.report),'--launch-config',str(self.launch))['dispatch']
                self.assertFalse(touched.exists())
                self.assertEqual(self.cli('close',dispatch)['result'],'completed')

    def test_blocked_host_rejects_enter_but_allows_interrupt(self):
        from unittest.mock import patch
        import herdr_backend
        dispatch = self.dispatch()
        handle = self.cli('status','--dispatch',dispatch)['dispatches'][0]['backendHandle']
        original = herdr_backend._snapshot_guard
        def blocked(snapshot, guard):
            pane = original(snapshot, guard)
            return {**pane, 'agent_status': 'blocked'}
        payload = {'endpoint':self.endpoint,'service':handle['service'],'guard':handle,
                   'method':'pane.send_keys','params':{'pane_id':handle['pane'],'keys':['Enter']}}
        with patch.object(herdr_backend, '_snapshot_guard', side_effect=blocked):
            result = herdr_backend.helper(payload)
            self.assertEqual((result['result'],result.get('category')),('refused','permission_required'))
            payload['params']['keys'] = ['C-c']
            self.assertEqual(herdr_backend.helper(payload)['result'],'accepted')
        self.assertEqual(self.cli('close',dispatch)['result'],'completed')

    def test_initial_prefill_and_concurrent_nudge_do_not_mix(self):
        gate = self.root / 'draw-composer'
        self.host.write_text(r'''import os,subprocess,sys,textwrap,time
from pathlib import Path
deadline=time.monotonic()+8
while not Path(GATE).exists() and time.monotonic()<deadline:time.sleep(.01)
if not Path(GATE).exists():raise TimeoutError('composer gate was not released')
text=sys.argv[sys.argv.index('--prompt')+1]
for line in textwrap.wrap(text,64):print('┃  '+line,flush=True)
print('┃\n┃  Build · controlled OpenCode\n╹'+'▀'*70,flush=True)
line=sys.stdin.readline()
if line.strip():raise RuntimeError('nudge mixed into initial prompt')
subprocess.run([sys.executable,PROGRAM,'ask','INITIAL_PROMPT_SUBMITTED',
                '--attempt',text.split('--attempt ')[1].split()[0]],check=True)
time.sleep(60)
'''.replace('PROGRAM',repr(str(PROGRAM))).replace('GATE',repr(str(gate))),encoding='utf-8')
        self.launch.write_text(json.dumps({'hosts':{'opencode':{'command':[str(self.host)],
                                      'runtime':[sys.executable,'-u']}}}),encoding='utf-8')
        child = subprocess.Popen([sys.executable,str(PROGRAM),'dispatch','--backend','herdr',
                                  '--herdr-endpoint',self.endpoint,'--host','opencode','--task',str(self.task),
                                  '--cwd',str(self.root),'--report',str(self.report),'--launch-config',str(self.launch)],
                                 env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        nudge = None
        try:
            deadline = time.monotonic()+5
            while time.monotonic()<deadline:
                rows = self.cli('status')['dispatches']
                if rows and rows[0].get('backendHandle',{}).get('state')=='active':break
                time.sleep(.02)
            self.assertTrue(rows)
            self.assertEqual(rows[0].get('backendHandle',{}).get('state'),'active')
            dispatch = rows[0]['dispatch']
            nudge = subprocess.Popen([sys.executable,str(PROGRAM),'nudge',dispatch,'--text','concurrent input'],
                                     env=self.env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
            time.sleep(.4)
            gate.touch()
            out,err=child.communicate(timeout=15)
            self.assertEqual(child.returncode,0,err)
            _,err=nudge.communicate(timeout=15)
            self.assertEqual(nudge.returncode,0,err)
            messages=self.cli('wait','--timeout','1')['messages']
            self.assertTrue(any(m.get('question')=='INITIAL_PROMPT_SUBMITTED' for m in messages),messages)
            self.assertEqual(self.cli('close',dispatch)['result'],'completed')
        finally:
            for process in (child,nudge):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_create_read_and_close_use_owned_persistent_unit(self):
        dispatch = self.dispatch()
        view = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        self.assertEqual(view['backend'], 'herdr')
        self.assertEqual(view['backendResult'], 'accepted')
        self.assertTrue(view['backendAlive'])
        handle = view['backendHandle']
        self.assertEqual(handle['attempt'], view['attempt'])
        self.assertTrue(handle['service']['creation'])
        closed = self.cli('close', dispatch)
        self.assertEqual(closed['result'], 'completed')

    def interactive_host(self):
        program = str(PROGRAM)
        self.host.write_text(r'''import json,os,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
state=Path(os.environ['WFC_STATE']); dispatch=os.environ['WFC_DISPATCH']
program=PROGRAM

def current():
    with sqlite3.connect(state/'router.db') as db:
        db.row_factory=sqlite3.Row
        return dict(db.execute('SELECT * FROM dispatch WHERE id=?',(dispatch,)).fetchone())
row=current()
# A controlled conversation record lets the real core resolve a resume identity.
sessions=Path(os.environ['CODEX_HOME'])/'sessions';sessions.mkdir(parents=True,exist_ok=True)
record=sessions/('rollout-test-'+dispatch+'.jsonl')
if not record.exists():
    record.write_text(json.dumps({'type':'session_meta','payload':{'id':dispatch,'source':'cli'}})+'\n'+json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':sys.argv[-1]}]}})+'\n',encoding='utf-8')
subprocess.run([sys.executable,program,'ask','May I finish?','--attempt',row['current_attempt']],check=True)
finished=False
for line in sys.stdin:
    if 'exit-controlled' in line:break
    previous=row['current_round']
    deadline=time.monotonic()+5
    while True:
        row=current()
        if not finished or row['current_round']>previous:break
        if time.monotonic()>deadline:raise RuntimeError('next round not committed')
        time.sleep(.01)
    Path(row['report']).write_text('# controlled result\n',encoding='utf-8')
    subprocess.run([sys.executable,program,'done','--report',row['report'],'--attempt',row['current_attempt']],check=True)
    finished=True
'''.replace('PROGRAM', repr(program)), encoding='utf-8')
    def test_question_reply_done_continue_and_restart_keep_round_identity(self):
        self.interactive_host()
        dispatch = self.dispatch()
        [question] = self.cli('wait', '--timeout', '5')['messages']
        self.assertEqual(question['type'], 'question')
        self.cli('reply', str(question['id']), 'finish now')
        [done] = self.cli('wait', '--timeout', '5')['messages']
        self.assertEqual(done['type'], 'done')
        first = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        second_report = self.root / 'round two.md'
        self.cli('continue', dispatch, '--task', str(self.task), '--report', str(second_report))
        [done2] = self.cli('wait', '--timeout', '5')['messages']
        self.assertEqual(done2['type'], 'done')
        second = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        self.assertEqual(second['round'], 2)
        self.assertNotEqual(first['attempt'], second['attempt'])
        self.assertEqual(second['backendHandle']['service'], first['backendHandle']['service'])
        # Same live terminal serves another round; its handle ownership still names its creating attempt.
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        cleaned = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        self.assertEqual(cleaned['rounds'], second['rounds'])
        self.assertEqual((cleaned['status'], cleaned['backendAlive']), ('completed', False))

    def test_closing_own_unit_leaves_shared_pane_running(self):
        dispatch = self.dispatch()
        handle = self.cli('status', '--dispatch', dispatch)['dispatches'][0]['backendHandle']
        self.api('pane.split', {'pane_id': handle['pane'], 'direction': 'right', 'cwd': str(self.root), 'focus': False})
        before = self.api('session.snapshot')['snapshot']['panes']
        other = [p['pane_id'] for p in before if p['workspace_id'] == handle['workspace'] and p['pane_id'] != handle['pane']]
        self.assertEqual(len(other), 1)
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        after = self.api('session.snapshot')['snapshot']['panes']
        self.assertIn(other[0], [p['pane_id'] for p in after])

    def test_service_restart_stays_unknown_without_replaying_input(self):
        dispatch = self.dispatch()
        before = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        self.stop_server()
        self.server = subprocess.Popen([BINARY, 'server'], env=self.env, stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = subprocess.run([BINARY, 'status', 'server', '--json'], env=self.env, capture_output=True)
            if status.returncode == 0 and b'"running"' in status.stdout:
                break
            time.sleep(.05)
        after = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        self.assertEqual(after['backendResult'], 'uncertain')
        self.assertEqual(after['attempt'], before['attempt'])
        self.assertEqual(after['backendHandle'], before['backendHandle'])
        result = subprocess.run([sys.executable, str(PROGRAM), 'nudge', dispatch, '--text', 'must not replay'],
                                env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('identity changed', result.stderr)
        self.assertEqual(self.cli('status', '--dispatch', dispatch)['dispatches'][0]['attempt'], before['attempt'])
        handle = before['backendHandle']
        # Herdr 0.9.5 may restore IDs without WFC's transient metadata. Never adopt that unit.
        snapshot = self.api('session.snapshot')['snapshot']
        workspace = next((w for w in snapshot['workspaces'] if w['workspace_id'] == handle['workspace']), None)
        matches = workspace is not None and workspace.get('tokens', {}).get('wfc_owner') == handle['nonce']
        rebound = subprocess.run([sys.executable, str(PROGRAM), 'herdr-reconcile', dispatch,
                                  '--pane', handle['pane'], '--terminal', handle['terminal'], '--actor', 'operator',
                                  '--evidence', 'inspected restarted service', '--old-writer-evidence', 'old service reaped'],
                                 env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=15)
        if workspace is not None and not matches:
            self.assertNotEqual(rebound.returncode, 0)
            self.assertIn('herdr_ownership', rebound.stderr)
            self.assertEqual(self.cli('status', '--dispatch', dispatch)['dispatches'][0]['backendHandle'], handle)
        else:
            self.assertEqual(rebound.returncode, 0, rebound.stderr)
            self.assertFalse(json.loads(rebound.stdout)['inputReplayed'])

    def test_read_only_host_refuses_input(self):
        self.host.write_text('import time\nprint("This conversation is unavailable. Its cached transcript and draft remain here; input is paused.",flush=True)\ntime.sleep(60)\n', encoding='utf-8')
        dispatch = self.dispatch()
        result = subprocess.run([sys.executable, str(PROGRAM), 'nudge', dispatch, '--text', 'never submit'],
                                env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('refused', result.stderr)
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')

    @unittest.skipIf(os.name == 'nt', 'Unix socket fault proxy; native pipe uses the same result contract')
    def test_lost_input_response_is_unknown_and_not_replayed(self):
        import socket
        import threading
        real_endpoint = self.endpoint
        listener = socket.socket(socket.AF_UNIX)
        proxy = str(self.root / 'proxy.sock')
        listener.bind(proxy)
        os.chmod(proxy, 0o600)
        listener.listen()
        listener.settimeout(.1)
        stopped = threading.Event()
        calls = []
        handlers = []

        def forward(client):
            try:
                with client, client.makefile('rwb') as stream:
                    raw = stream.readline(2 * 1024 * 1024)
                    method = json.loads(raw)['method']
                    with socket.socket(socket.AF_UNIX) as target:
                        target.settimeout(5)
                        target.connect(real_endpoint)
                        target.sendall(raw)
                        with target.makefile('rb') as incoming:
                            response = incoming.readline(2 * 1024 * 1024)
                    if method == 'pane.send_input':
                        calls.append(method)
                        stopped.wait(3)  # backend accepted; its response is lost
                        return
                    stream.write(response)
                    stream.flush()
            except OSError:
                pass

        def serve():
            while not stopped.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                handler = threading.Thread(target=forward, args=(client,))
                handlers.append(handler)
                handler.start()

        thread = threading.Thread(target=serve)
        thread.start()
        self.endpoint = proxy
        try:
            dispatch = self.dispatch()
            before = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
            self.env['WFC_HERDR_TIMEOUT'] = '1'
            result = subprocess.run([sys.executable, str(PROGRAM), 'nudge', dispatch, '--text', 'one intent'],
                                    env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('uncertain', result.stderr)
            self.env.pop('WFC_HERDR_TIMEOUT')
            for _ in range(2):
                view = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
                self.assertEqual(view['attempt'], before['attempt'])
                self.cli('wait', '--timeout', '0')
            self.assertEqual(calls, ['pane.send_input'])
            self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        finally:
            stopped.set()
            listener.close()
            thread.join(timeout=5)
            for handler in handlers:
                handler.join(timeout=5)

    def test_interrupt_close_and_resume_fence_the_previous_attempt(self):
        self.interactive_host()
        dispatch = self.dispatch()
        first = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        interrupted = self.cli('interrupt', dispatch)
        self.assertEqual(interrupted['result'], 'accepted')
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        resumed = self.cli('resume', dispatch)
        self.assertEqual(resumed['result'], 'accepted')
        second = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        self.assertNotEqual(first['attempt'], second['attempt'])
        self.assertNotEqual(first['backendHandle']['terminal'], second['backendHandle']['terminal'])
        questions = [message for message in second['messages'] if message['type'] == 'question']
        self.assertIn(second['attempt'], [question['attempt'] for question in questions])
        late = subprocess.run([sys.executable, str(PROGRAM), 'ask', 'late question',
                               '--attempt', first['attempt']],
                              env={**self.env, 'WFC_DISPATCH': dispatch}, capture_output=True,
                              text=True, encoding='utf-8', timeout=15)
        self.assertNotEqual(late.returncode, 0)
        self.assertIn('no longer accepted', late.stderr)
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')

    def test_explicit_unit_binding_preserves_owner_fence_and_does_not_replay(self):
        scope = 'test-herdr-owner'
        owner = self.cli('owner', 'acquire', '--scope', scope, '--owner', 'first')
        dispatch = self.dispatch('--scope', scope, '--owner-token', owner['token'])
        first = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
        handle = first['backendHandle']
        bound = self.cli('herdr-reconcile', dispatch, '--pane', handle['pane'], '--terminal', handle['terminal'],
                         '--actor', 'operator', '--evidence', 'inspected same owned unit', '--owner-token', owner['token'])
        self.assertFalse(bound['inputReplayed'])
        self.assertEqual(bound['attempt'], first['attempt'])
        next_owner = self.cli('owner', 'takeover', '--scope', scope, '--owner', 'second')
        refused = subprocess.run([sys.executable, str(PROGRAM), 'herdr-reconcile', dispatch,
                                  '--pane', handle['pane'], '--terminal', handle['terminal'], '--actor', 'old owner',
                                  '--evidence', 'stale', '--owner-token', owner['token']], env=self.env,
                                 capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('stale_controller_owner', refused.stderr)
        self.assertEqual(self.cli('close', dispatch, '--owner-token', next_owner['token'])['result'], 'completed')
