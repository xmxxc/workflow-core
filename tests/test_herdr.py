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
from unittest.mock import patch

import herdr_backend

ROOT = Path(__file__).resolve().parent.parent
BINARY = os.environ.get('WFC_HERDR_TEST_BINARY')
PROGRAM = Path(os.environ.get('WFC_TEST_PROGRAM', ROOT / 'wfc.py')).resolve()


class CodexSubmission(unittest.TestCase):
    text = '[router] Answer to question 1: finish the controlled task exactly once'

    def screen(self, draft):
        return '› ' + draft + '\n\n  gpt-controlled · test\n'

    def submit(self, screens, results=None, host='codex', before=None):
        backend = herdr_backend.HerdrBackend()
        observations = [{'result': 'accepted', 'screen': before or self.screen('')}] + [
            {'result': 'accepted', 'screen': s} if isinstance(s, str) else s for s in screens]
        with patch.object(backend, 'read', side_effect=observations) as read, \
                patch.object(backend, '_handle', return_value={'pane': 'owned-pane'}), \
                patch.object(backend, '_call', side_effect=results or [{'result': 'accepted'}] * 4) as call, \
                patch.object(herdr_backend.time, 'sleep'):
            outcome = backend.submit('d-test', self.text, host)
        return outcome, call.call_args_list, read.call_count

    def test_stable_complete_draft_gets_only_enter_then_empty_box_stops(self):
        full = self.screen(self.text)
        outcome, calls, reads = self.submit([full, full, self.screen(''), self.screen('')])
        self.assertEqual(outcome['result'], 'accepted')
        self.assertEqual(reads, 5)
        self.assertEqual([c.args[1] for c in calls], ['pane.send_input', 'pane.send_keys'])
        self.assertEqual(calls[0].args[2]['text'], self.text)
        self.assertEqual(calls[1].args[2], {'pane_id': 'owned-pane', 'keys': ['Enter']})
        self.assertTrue(all(c.kwargs['guard'] for c in calls))

    def test_empty_other_overlapping_scrolled_unreadable_or_changing_drafts_get_no_key(self):
        for first, second in (
                ('', ''), ('Ask Codex to do anything', 'Ask Codex to do anything'),
                ('unrelated draft', 'unrelated draft'), (self.text[:30], self.text[:30]),
                (self.text + ' extra', self.text + ' extra'),
                (self.text + ' ↑', self.text + ' ↑'),
                (self.text, self.text + ' changed')):
            with self.subTest(first=first, second=second):
                outcome, calls, _ = self.submit([self.screen(first), self.screen(second)])
                self.assertEqual(outcome['result'], 'accepted')
                self.assertEqual(len(calls), 1)
        outcome, calls, _ = self.submit(['transcript only', 'transcript only'])
        self.assertEqual((outcome['result'], len(calls)), ('accepted', 1))

    def test_observation_failure_or_read_only_after_text_stays_uncertain(self):
        for observation in ({'result': 'uncertain', 'category': 'herdr_restart'},
                            {'result': 'refused'}, {'result': 'definitely-not-sent'},
                            'This conversation is unavailable. Its cached transcript and draft remain here; input is paused.'):
            with self.subTest(observation=observation):
                outcome, calls, _ = self.submit([observation])
                self.assertEqual((outcome['result'], len(calls)), ('uncertain', 1))

    def test_either_scroll_direction_in_either_observation_gets_no_recovery_key(self):
        full = self.screen(self.text)
        for marker in ('↑', '↓'):
            clipped = self.screen(self.text + ' ' + marker)
            for frames in ([clipped] * 6, [clipped, full] + [full] * 4,
                           [full, clipped] + [full] * 4):
                with self.subTest(marker=marker, first=frames[0], second=frames[1]):
                    outcome, calls, _ = self.submit(frames)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(outcome['result'], 'accepted')

    def test_complete_box_reader_preserves_default_scroll_contract(self):
        import router
        for marker in ('↑', '↓'):
            with self.subTest(marker=marker):
                frame = self.screen(self.text + ' ' + marker)
                self.assertEqual(router.codex_input_box(frame), router.InputBox(self.text, marker == '↑'))
                self.assertIsNone(router.codex_input_box(frame, require_complete=True))
                # Transcript arrows do not crop the composer below them.
                transcript = '› earlier message ' + marker + '\n\n' + self.screen(self.text)
                self.assertEqual(router.codex_input_box(transcript, require_complete=True),
                                 router.InputBox(self.text, False))

    def test_unknown_initial_send_never_reads_or_sends_again(self):
        outcome, calls, reads = self.submit([], [{'result': 'uncertain'}])
        self.assertEqual((outcome['result'], len(calls), reads), ('uncertain', 1, 1))

    def test_new_refusal_after_input_or_followup_enter_stops_keys(self):
        full = self.screen(self.text)
        refused = 'This conversation is read-only or unavailable; no operation was sent.\n' + full
        for screens, writes in (([refused] * 6, 1), ([full, full] + [refused] * 4, 2)):
            with self.subTest(writes=writes):
                outcome, calls, _ = self.submit(screens)
                self.assertEqual(outcome['result'], 'uncertain')
                self.assertEqual(len(calls), writes)
                self.assertEqual(outcome['category'], 'permission_required')

    def test_old_transcript_refusal_does_not_prevent_recovered_enter(self):
        refusal = 'This conversation is read-only or unavailable; no operation was sent.\n'
        full = refusal + self.screen(self.text)
        empty = refusal + self.screen('')
        outcome, calls, _ = self.submit([full, full, empty, empty], before=empty)
        self.assertEqual((outcome['result'], len(calls)), ('accepted', 2))

    def test_failed_followup_key_never_replays_text_or_key(self):
        for result in ('uncertain', 'refused', 'definitely-not-sent'):
            with self.subTest(result=result):
                full = self.screen(self.text)
                outcome, calls, reads = self.submit([full, full],
                    [{'result': 'accepted'}, {'result': result, 'category': 'herdr_restart'}])
                self.assertEqual((outcome['result'], len(calls), reads), ('uncertain', 2, 3))
                self.assertEqual(outcome['category'], 'herdr_restart')
        full = self.screen(self.text)
        outcome, calls, _ = self.submit([full, full], [{'result': 'accepted'}, {'result': 'accepted', 'absent': True}])
        self.assertEqual((outcome['result'], len(calls)), ('uncertain', 2))

    def test_persistently_stuck_draft_is_bounded_and_uncertain(self):
        outcome, calls, _ = self.submit([self.screen(self.text)] * 6)
        self.assertEqual((outcome['result'], len(calls)), ('uncertain', 3))

    def test_other_hosts_keep_one_combined_send(self):
        for host in ('opencode', 'claude', None):
            outcome, calls, reads = self.submit([], host=host)
            self.assertEqual((outcome['result'], len(calls), reads), ('accepted', 1, 1))


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
        evidence = {'pid': self.server.pid, 'poll_before': self.server.poll(),
                    'taskkill': None, 'reaped_returncode': None}
        try:
            if evidence['poll_before'] is None:
                if os.name == 'nt':
                    # This Popen owns the isolated service; retain tree cleanup and its diagnostics.
                    command = ['taskkill', '/PID', str(self.server.pid), '/T', '/F']
                    try:
                        result = subprocess.run(command, capture_output=True, timeout=10, check=True)
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                        evidence['taskkill'] = {'command': command, 'returncode': getattr(error, 'returncode', None),
                                                'stdout': repr(error.output), 'stderr': repr(error.stderr),
                                                'timed_out': isinstance(error, subprocess.TimeoutExpired)}
                        try:
                            evidence['reaped_returncode'] = self.server.wait(timeout=0)
                        except subprocess.TimeoutExpired:
                            evidence['wait_timed_out'] = True
                        # Root exit alone cannot prove that /T cleaned all descendants.
                        # Keep the failure until native evidence establishes a benign case.
                        raise AssertionError('Herdr fixture tree cleanup unconfirmed: ' + json.dumps(evidence)) from error
                    evidence['taskkill'] = {'command': command, 'returncode': result.returncode,
                                            'stdout': repr(result.stdout), 'stderr': repr(result.stderr)}
                else:
                    self.server.terminate()
                evidence['reaped_returncode'] = self.server.wait(timeout=10)
            else:
                evidence['reaped_returncode'] = self.server.wait(timeout=0)
        finally:
            print('[herdr-fixture-stop] ' + json.dumps(evidence), file=sys.stderr)

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

    def codex_composer_host(self, drop_enter=True):
        """A raw terminal with Codex's composer and burst-Enter suppression.

        Herdr's send_input writes bracketed paste and Enter in one operation.
        Record host submissions separately from the backend's acceptance receipt.
        """
        events = self.root / 'host-events.jsonl'
        self.host.write_text(r'''import json,os,subprocess,sys,termios,time,tty
from pathlib import Path
tty.setraw(sys.stdin.fileno())
text=''; pending=''; pasted_at=0
def draw():
    print('\x1b[2J\x1b[H› '+text+'\r\n\r\n  gpt-controlled · test\r\n',end='',flush=True)
subprocess.run([sys.executable,PROGRAM,'ask','COMPOSER_READY',
               '--attempt',sys.argv[-1].split('--attempt ')[1].split()[0]],stdout=subprocess.DEVNULL,check=True)
print('\x1b[?2004h',end='',flush=True)
draw()
while True:
    pending+=os.read(sys.stdin.fileno(),65536).decode('utf-8')
    while pending:
        if pending.startswith('\x1b[200~'):
            end=pending.find('\x1b[201~')
            if end<0:break
            text+=pending[6:end];pending=pending[end+6:]
            pasted_at=time.monotonic();draw()
        else:
            char,pending=pending[0],pending[1:]
            if char in '\r\n':
                if DROP and time.monotonic()-pasted_at<.15:
                    with Path(EVENTS).open('a') as f:f.write(json.dumps({'dropped':True,'draft':text})+'\n')
                    continue
                with Path(EVENTS).open('a') as f:f.write(json.dumps({'user':text})+'\n')
                text='';draw()
            elif char=='\x03':sys.exit(0)
            else:text+=char;pasted_at=time.monotonic();draw()
'''.replace('PROGRAM', repr(str(PROGRAM))).replace('EVENTS', repr(str(events)))
            .replace('DROP', repr(drop_enter)), encoding='utf-8')
        return events

    @unittest.skipIf(os.name == 'nt', 'raw POSIX controlled TUI; Windows live retest is separate')
    def test_public_reply_submits_a_codex_draft_after_burst_enter_is_dropped(self):
        events = self.codex_composer_host()
        dispatch = self.dispatch()
        try:
            [question] = self.cli('wait', '--timeout', '5')['messages']
            self.assertEqual(question['question'], 'COMPOSER_READY')
            answer = 'finish the controlled task exactly once'
            self.cli('reply', str(question['id']), answer)
            time.sleep(.1)
            recorded = [json.loads(line) for line in events.read_text().splitlines()]
            self.assertEqual(len([e for e in recorded if e.get('dropped')]), 1, recorded)
            users = [e['user'] for e in recorded if 'user' in e]
            self.assertEqual(len(users), 1, recorded)
            self.assertIn(answer, users[0])
        finally:
            self.assertEqual(self.cli('close', dispatch)['result'], 'completed')

    @unittest.skipIf(os.name == 'nt', 'raw POSIX controlled TUI; Windows live retest is separate')
    def test_public_reply_already_submitted_gets_no_empty_enter(self):
        events = self.codex_composer_host(drop_enter=False)
        dispatch = self.dispatch()
        try:
            [question] = self.cli('wait', '--timeout', '5')['messages']
            self.cli('reply', str(question['id']), 'submit once')
            recorded = [json.loads(line) for line in events.read_text().splitlines()]
            self.assertEqual(recorded, [{'user': f"[router] Answer to question {question['id']}: submit once"}])
        finally:
            self.assertEqual(self.cli('close', dispatch)['result'], 'completed')

    @unittest.skipIf(os.name == 'nt', 'raw POSIX controlled TUI; Windows live retest is separate')
    def test_public_reply_and_concurrent_nudge_do_not_mix_codex_drafts(self):
        events = self.codex_composer_host()
        dispatch = self.dispatch()
        children = []
        try:
            [question] = self.cli('wait', '--timeout', '5')['messages']
            for command in (['reply', str(question['id']), 'reply marker'],
                            ['nudge', dispatch, '--text', 'nudge marker']):
                children.append(subprocess.Popen([sys.executable, str(PROGRAM), *command], env=self.env,
                                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE))
                time.sleep(.1)
            for child in children:
                out, err = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, err.decode())
            users = [event['user'] for line in events.read_text().splitlines()
                     if 'user' in (event := json.loads(line))]
            self.assertEqual(len(users), 2, users)
            self.assertEqual(sum('reply marker' in text for text in users), 1, users)
            self.assertEqual(sum('nudge marker' in text for text in users), 1, users)
            self.assertFalse(any('reply marker' in text and 'nudge marker' in text for text in users), users)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()
            self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
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
        self.lost_response('pane.send_input')

    @unittest.skipIf(os.name == 'nt', 'raw POSIX TUI and Unix socket fault proxy')
    def test_lost_followup_enter_response_keeps_public_reply_claimed(self):
        events = self.codex_composer_host()
        self.lost_response('pane.send_keys', public_reply=True)
        users = [event['user'] for line in events.read_text().splitlines()
                 if 'user' in (event := json.loads(line))]
        self.assertEqual(len(users), 1, users)
        self.assertIn('one intent', users[0])

    def lost_response(self, lost_method, public_reply=False):
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
                    if method in ('pane.send_input', 'pane.send_keys'):
                        calls.append(method)
                    if method == lost_method:
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
            if public_reply:
                [question] = self.cli('wait', '--timeout', '5')['messages']
                command = ['reply', str(question['id']), 'one intent']
            else:
                command = ['nudge', dispatch, '--text', 'one intent']
            self.env['WFC_HERDR_TIMEOUT'] = '1'
            result = subprocess.run([sys.executable, str(PROGRAM), *command],
                                    env=self.env, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('uncertain', result.stderr)
            self.env.pop('WFC_HERDR_TIMEOUT')
            for _ in range(2):
                view = self.cli('status', '--dispatch', dispatch)['dispatches'][0]
                self.assertEqual(view['attempt'], before['attempt'])
                self.cli('wait', '--timeout', '0')
            if public_reply:
                retry = subprocess.run([sys.executable, str(PROGRAM), *command], env=self.env,
                                       capture_output=True, text=True, encoding='utf-8', timeout=15)
                self.assertNotEqual(retry.returncode, 0)
                self.assertIn('already answered', retry.stderr)
            self.assertEqual(calls, ['pane.send_input', 'pane.send_keys'] if public_reply else ['pane.send_input'])
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
