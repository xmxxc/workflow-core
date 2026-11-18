"""Bundled backend contracts; native acceptance is opt-in via a real kernel path."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BundledSelection(unittest.TestCase):
    def test_missing_companion_is_explicit_and_does_not_launch_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            task = directory / 'task.md'
            task.write_text('controlled task', encoding='utf-8')
            result = subprocess.run([sys.executable, str(ROOT / 'wfc.py'), 'dispatch', '--backend', 'bundled',
                                     '--host', 'codex', '--cwd', tmp, '--task', str(task),
                                     '--report', str(directory / 'report.md')], capture_output=True, text=True,
                                    env={**os.environ, 'WFC_STATE': str(directory / 'state'),
                                         'WFC_BUNDLED_KERNEL': str(directory / 'missing-kernel')}, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('bundled kernel companion', result.stderr)
            self.assertNotIn('invalid choice', result.stderr)


if __name__ == '__main__':
    unittest.main()

class BundledPlatform(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux containment contract')
    def test_dispatch_requires_delegated_cgroup_before_host_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            task = directory / 'task.md'
            task.write_text('task', encoding='utf-8')
            env = {**os.environ, 'WFC_STATE': str(directory / 'state'), 'WFC_BUNDLED_KERNEL': sys.executable}
            env.pop('WFC_BUNDLED_CGROUP_ROOT', None)
            result = subprocess.run([sys.executable, str(ROOT / 'wfc.py'), 'dispatch', '--backend', 'bundled',
                                     '--host', 'codex', '--cwd', tmp, '--task', str(task),
                                     '--report', str(directory / 'report.md')], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('delegated cgroup v2', result.stderr)
            self.assertFalse((directory / 'state' / 'bundled').exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux containment contract')
    def test_directory_lookalike_is_not_accepted_as_cgroup(self):
        import bundled_runtime
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'cgroup.kill').touch()
            with self.assertRaisesRegex(OSError, 'cgroup v2 filesystem'):
                bundled_runtime.cgroup_root(tmp)


@unittest.skipUnless(os.environ.get('WFC_BUNDLED_TEST_KERNEL'), 'set WFC_BUNDLED_TEST_KERNEL for real native acceptance')
class BundledNative(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wfc bundled 验证 ')
        self.root = Path(self.tmp.name)
        self.program = Path(os.environ.get('WFC_TEST_PROGRAM', ROOT / 'wfc.py')).resolve()
        self.report = self.root / 'report.md'
        self.task = self.root / 'task.md'
        self.task.write_text('controlled worker; no account', encoding='utf-8')
        self.evidence = self.root / 'descendant.pid'
        self.launch = self.root / 'launch.json'
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ('WFC_DISPATCH', 'WFC_OWNER_TOKEN', 'WFC_STATE', 'WFC_LAUNCH_CONFIG')}
        self.env.update(WFC_STATE=str(self.root / 'state'), CODEX_HOME=str(self.root / 'codex-home'), PYTHONUTF8='1',
                        WFC_BUNDLED_KERNEL=str(Path(os.environ['WFC_BUNDLED_TEST_KERNEL']).resolve()))
        if os.environ.get('WFC_BUNDLED_TEST_DISCOVERY'):
            self.env.pop('WFC_BUNDLED_KERNEL', None)
        self.dispatches = []
        self.identities = []
        self.config()

    def config(self, mode=''):
        self.launch.write_text(json.dumps({'hosts': {'codex': {
            'runtime': [sys.executable, '-u'], 'command': [str(ROOT / 'tests/fixtures/bundled_worker.py')],
            'env': {'WFC_TEST_PROGRAM': str(self.program), 'WFC_TEST_REPORT': str(self.report),
                    'WFC_TEST_EVIDENCE': str(self.evidence), 'WFC_TEST_MODE': mode}}}}), encoding='utf-8')

    def cli(self, *args, success=True):
        value = subprocess.run([sys.executable, str(self.program), *args], env=self.env,
                               capture_output=True, text=True, encoding='utf-8', timeout=25)
        if success:
            self.assertIn(value.returncode, (0, 3), value.stderr + value.stdout)
            return json.loads(value.stdout)
        return value

    def dispatch(self, mode=''):
        self.config(mode)
        value = self.cli('dispatch', '--backend', 'bundled', '--host', 'codex', '--task', str(self.task),
                         '--cwd', str(self.root), '--report', str(self.report), '--launch-config', str(self.launch))
        self.dispatches.append(value['dispatch'])
        self.assertEqual(value['backend'], 'bundled')
        return value

    def status(self, dispatch):
        return self.cli('status', '--dispatch', dispatch)['dispatches'][0]

    def until(self, predicate, timeout=10):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.05)
        self.fail('native acceptance observation deadline exceeded')

    def handle(self, dispatch):
        handle = self.status(dispatch)['backendHandle']
        self.assertNotIn('token', handle)
        self.assertEqual(handle['state'], 'active')
        self.assertTrue(handle['service']['creation'])
        self.assertTrue(handle['kernel']['creation'])
        self.assertTrue(handle['guardian']['creation'])
        self.identities.extend([handle['service'], handle['kernel'], handle['guardian']])
        import bundled_runtime
        return bundled_runtime.read_json(handle['snapshot'])

    def tearDown(self):
        import time
        import wfc_platform
        import bundled_runtime
        for dispatch in self.dispatches:
            self.cli('close', dispatch, success=False)
        # Direct capability-guarded cleanup also covers a failed CLI create/close.
        for path in (self.root / 'state' / 'bundled').glob('*.json'):
            try:
                snapshot = bundled_runtime.read_json(path)
                self.identities.extend(snapshot[key] for key in ('service', 'kernel', 'guardian') if snapshot.get(key))
                if snapshot.get('state') == 'active' and snapshot.get('token'):
                    bundled_runtime.call(snapshot, 'close')
            except (OSError, ValueError):
                pass
        # Every test-owned process must stop before removing its private evidence.
        survivors = []
        for identity in self.identities:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                probe = wfc_platform.process_probe(identity['pid'])
                if probe['state'] == 'exited' or probe['identity'] != identity['creation']:
                    break
                time.sleep(.05)
            else:
                # Failure cleanup is test-only and fenced by creation identity.
                import signal
                if wfc_platform.process_identity(identity['pid']) == identity['creation']:
                    os.kill(identity['pid'], signal.SIGTERM if os.name == 'nt' else signal.SIGKILL)
                survivors.append(identity)
        self.tmp.cleanup()
        self.assertFalse(survivors, 'test-owned processes required emergency cleanup: ' + str(survivors))

    def test_standalone_dispatch_reply_done_and_close(self):
        import bundled_runtime
        value = self.dispatch()
        dispatch = value['dispatch']
        handle = self.handle(dispatch)
        view = self.until(lambda: (lambda text: text if 'RIGHT' in text and '"tty": [true, true]' in text else None)(
            bundled_runtime.call(handle, 'read').get('data', {}).get('text', '')))
        self.assertIn('RIGHT', view)
        self.assertIn('"tty": [true, true]', view)
        messages = self.cli('wait', '--timeout', '5')['messages']
        question = next(item for item in messages if item.get('question') == 'controlled question')
        self.cli('reply', str(question['id']), 'finish now')
        self.until(lambda: self.report.exists())
        messages = self.cli('wait', '--timeout', '5')['messages']
        done = next(item for item in messages if item['type'] == 'done')
        self.assertEqual(done['attempt'], value['attempt'])
        self.assertEqual(done['state'], 'received')
        received = next(item for item in self.status(dispatch)['messages'] if item['messageId'] == done['messageId'])
        self.assertEqual(received['state'], 'received')
        self.assertTrue(received['delivered'])
        ack = self.cli('ack', done['messageId'])
        self.assertEqual(ack['state'], 'processed')
        self.assertFalse(ack['duplicate'])
        processed = next(item for item in self.status(dispatch)['messages'] if item['messageId'] == done['messageId'])
        self.assertEqual(processed['state'], 'processed')
        self.until(lambda: self.status(dispatch)['status'] == 'completed')
        self.assertEqual(self.status(dispatch)['attempt'], value['attempt'])
        self.assertEqual(Path(str(self.evidence) + '.applied').read_text(), 'once\n')
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        self.assertFalse(self.status(dispatch)['backendAlive'])

    def test_wrong_nonce_and_generation_cannot_input_or_close(self):
        import bundled_runtime
        value = self.dispatch()
        handle = self.handle(value['dispatch'])
        for changed in ({'nonce': 'wrong'}, {'generation': 'wrong'}, {'attempt': 'wrong'}, {'unit': 'wrong'}):
            for op in ('input', 'close'):
                result = bundled_runtime.call({**handle, **changed}, op, text='unsafe\r')
                self.assertIn(result['result'], ('refused', 'uncertain'), result)
        self.assertTrue(self.status(value['dispatch'])['backendAlive'])
        self.assertFalse(self.report.exists())

    def test_interrupt_close_resume_replaces_execution_identity(self):
        value = self.dispatch()
        dispatch = value['dispatch']
        first = self.handle(dispatch)
        self.assertEqual(self.cli('interrupt', dispatch)['result'], 'accepted')
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        resumed = self.cli('resume', dispatch)
        self.assertNotEqual(resumed['attempt'], value['attempt'])
        second = self.handle(dispatch)
        self.assertNotEqual(first['generation'], second['generation'])
        self.assertNotEqual(first['nonce'], second['nonce'])
        late = subprocess.run([sys.executable, str(self.program), 'ask', 'stale', '--attempt', value['attempt']],
                              env={**self.env, 'WFC_DISPATCH': dispatch}, capture_output=True, timeout=15)
        self.assertNotEqual(late.returncode, 0)

    def descendant_identity(self):
        import wfc_platform
        self.until(lambda: self.evidence.exists() and self.evidence.read_text())
        identity = json.loads(self.evidence.read_text())
        self.assertTrue(identity['creation'])
        self.identities.append(identity)
        return identity

    def assert_stopped(self, identity):
        import wfc_platform
        self.until(lambda: wfc_platform.process_probe(identity['pid'])['state'] == 'exited'
                   or wfc_platform.process_identity(identity['pid']) != identity['creation'])

    def test_close_contains_setsid_descendant(self):
        value = self.dispatch('tree')
        self.handle(value['dispatch'])
        child = self.descendant_identity()
        self.assertEqual(self.cli('close', value['dispatch'])['result'], 'completed')
        self.assert_stopped(child)

    def test_parent_first_exit_cleans_descendant_and_allows_resume(self):
        value = self.dispatch('parent-exit')
        child = self.descendant_identity()
        self.until(lambda: not self.status(value['dispatch'])['backendAlive'])
        self.assert_stopped(child)
        self.config()
        self.assertEqual(self.cli('resume', value['dispatch'])['result'], 'accepted')
        self.handle(value['dispatch'])

    def test_runtime_forced_death_cleans_descendants_without_restarting(self):
        import signal
        import wfc_platform
        value = self.dispatch('tree')
        handle = self.handle(value['dispatch'])
        child = self.descendant_identity()
        self.assertEqual(wfc_platform.process_identity(handle['service']['pid']), handle['service']['creation'])
        os.kill(handle['service']['pid'], signal.SIGTERM if os.name == 'nt' else signal.SIGKILL)
        self.assert_stopped(child)
        self.assert_stopped(handle['kernel'])
        if os.name == 'nt':
            # OS Job closure kills the tree, but dead runtime cannot sign a cleanup receipt.
            self.assertEqual(self.status(value['dispatch'])['status'], 'unknown')
        else:
            self.until(lambda: not self.status(value['dispatch'])['backendAlive'])
        self.assertEqual(self.status(value['dispatch'])['attempt'], value['attempt'])


class BundledTransport(unittest.TestCase):
    def test_lost_reply_is_uncertain_and_request_is_not_replayed(self):
        import hashlib
        import hmac
        import socket
        import threading
        import bundled_runtime
        import wfc_platform
        try:
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
        except PermissionError as error:
            self.skipTest(str(error))
        listener.listen(2)
        listener.settimeout(2)
        requests, failures = [], []
        handle = {'token': 'test capability', 'unit': 'd-test', 'attempt': 'a-test', 'nonce': 'test nonce',
                  'generation': 'test generation', 'endpoint': listener.getsockname()[1],
                  'service': {'pid': os.getpid(), 'creation': wfc_platform.process_identity(os.getpid())}}
        def server():
            try:
                with listener.accept()[0] as stream:
                    stream.settimeout(2)
                    challenge = bundled_runtime.receive(stream)['challenge']
                    proof = hmac.new(handle['token'].encode(), ('server:' + challenge).encode(), hashlib.sha256).hexdigest()
                    stream.sendall(bundled_runtime.encode({'proof': proof}))
                    requests.append(bundled_runtime.receive(stream))
                    # Applied once, then connection closes before the acknowledgement.
            except Exception as error:
                failures.append(error)
        thread = threading.Thread(target=server)
        thread.start()
        try:
            value = bundled_runtime.call(handle, 'input', text='one intent')
            self.assertEqual(value['result'], 'uncertain')
        finally:
            thread.join(timeout=4)
            listener.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures, failures)
        self.assertEqual([item['text'] for item in requests], ['one intent'])

    def test_stale_service_identity_is_unknown_before_connect(self):
        import bundled_runtime
        value = bundled_runtime.call({'service': {'pid': os.getpid(), 'creation': 'old creation'},
                                      'endpoint': 1}, 'close')
        self.assertEqual(value['result'], 'uncertain')
        self.assertEqual(value['category'], 'bundled_restart')
