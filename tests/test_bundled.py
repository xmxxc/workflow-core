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
                    if key not in ('WFC_DISPATCH', 'WFC_OWNER_TOKEN', 'WFC_CONTROLLER_TOKEN', 'WFC_WORKER_CALLBACK', 'WFC_STATE', 'WFC_LAUNCH_CONFIG')}
        self.env.update(WFC_STATE=str(self.root / 'state'), CODEX_HOME=str(self.root / 'codex-home'), PYTHONUTF8='1',
                        WFC_BUNDLED_DIAGNOSTICS='1',
                        WFC_BUNDLED_KERNEL=str(Path(os.environ['WFC_BUNDLED_TEST_KERNEL']).resolve()))
        if os.environ.get('WFC_BUNDLED_TEST_DISCOVERY'):
            self.env.pop('WFC_BUNDLED_KERNEL', None)
        self.dispatches = []
        self.identities = []
        self.config()

    def config(self, mode=''):
        self.launch.write_text(json.dumps({'hosts': {'codex': {
            'runtime': [sys.executable, '-u'], 'command': [str(ROOT / 'tests/fixtures/bundled_worker.py')],
            'env': {'WFC_TEST_PROGRAM': str(getattr(self, 'worker_program', self.program)), 'WFC_TEST_REPORT': str(self.report),
                    'WFC_TEST_EVIDENCE': str(self.evidence), 'WFC_TEST_MODE': mode}}}}), encoding='utf-8')

    def cli(self, *args, success=True):
        value = subprocess.run([sys.executable, str(self.program), *args], env=self.env,
                               capture_output=True, text=True, encoding='utf-8', timeout=25)
        if success:
            self.assertIn(value.returncode, (0, 3), value.stderr + value.stdout)
            return json.loads(value.stdout)
        return value

    def dispatch(self, mode='', *extra):
        self.config(mode)
        value = self.cli('dispatch', '--backend', 'bundled', '--host', 'codex', '--task', str(self.task),
                         '--cwd', str(self.root), '--report', str(self.report), '--launch-config', str(self.launch), *extra)
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
            self.cli('close', dispatch, *getattr(self, 'owner_args', []), success=False)
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

    def test_inflight_worker_reports_after_takeover(self):
        import hashlib
        migration = os.environ.get('WFC_TEST_DOT3_LAUNCH')
        candidate = self.program
        legacy = os.environ.get('WFC_TEST_DOT3_BASELINE') == '1' or bool(migration)
        if migration:
            # The original process and environment survive. Its callback executable
            # and B's owner command now open the release-created DB using the candidate.
            self.program = Path(migration).resolve()
            self.worker_program = candidate
        if legacy:
            self.assertEqual(hashlib.sha256(self.program.read_bytes()).hexdigest(),
                             'e9b9c07384d2710ea59e163940706a5146ef60594a02057b80ca52c32b59bea6')
        scope = 'takeover-fixture'
        first = self.cli('owner', 'acquire', '--scope', scope, '--owner', 'A', '--lease', '120')
        self.env['WFC_CONTROLLER_TOKEN'] = first['token']
        value = self.dispatch('takeover', '--scope', scope)
        dispatch = value['dispatch']
        self.handle(dispatch)
        def evidence(action):
            path = Path(str(self.evidence) + '.' + action)
            self.until(lambda: path.exists() and path.stat().st_size)
            return json.loads(path.read_text(encoding='utf-8'))
        ready = evidence('ready')
        self.assertEqual(ready['attempt'], value['attempt'])
        before = self.status(dispatch)
        self.assertEqual((before['status'], before['lifecycle']), ('online', 'running'))
        self.assertFalse(self.report.exists())
        self.program = candidate
        second = self.cli('owner', 'takeover', '--scope', scope, '--owner', 'B', '--lease', '120')
        self.env['WFC_CONTROLLER_TOKEN'] = second['token']
        self.assertEqual(second['generation'], first['generation'] + 1)
        Path(str(self.evidence) + '.release').touch()
        ask = evidence('ask')
        if legacy:
            self.assertTrue(ready['ownerInHost'])
            done = evidence('done')
            for reply in (ask, done):
                self.assertEqual((reply['exit'], reply['error']), (1, 'stale_controller_owner'))
            view = self.status(dispatch)
            self.assertEqual((view['status'], view['lifecycle']), ('online', 'running'))
            self.assertFalse(any(m['type'] in ('question', 'done') for m in view['messages']))
            if migration:
                import sqlite3
                db = sqlite3.connect((self.root / 'state' / 'router.db').as_uri() + '?mode=ro', uri=True)
                try:
                    row = db.execute('SELECT callback_channel, callback_hash FROM dispatch_round '
                                     'WHERE dispatch=? AND attempt=?', (dispatch, value['attempt'])).fetchone()
                    self.assertTrue(row == (None, None), 'migration minted an unissued callback grant')
                finally:
                    db.close()
        else:
            self.assertFalse(ready['ownerInHost'])
            self.assertEqual(ask['exit'], 0)
            self.assertTrue(ask['stableRetry'])
            self.env['WFC_CONTROLLER_TOKEN'] = first['token']
            rejected = self.cli('wait', '--scope', scope, '--timeout', '1', success=False)
            self.assertIn('stale_controller_owner', rejected.stderr)
            rejected = self.cli('nudge', dispatch, '--text', 'unused', success=False)
            self.assertIn('stale_controller_owner', rejected.stderr)
            self.env['WFC_CONTROLLER_TOKEN'] = second['token']
            [question] = self.cli('wait', '--scope', scope, '--timeout', '5')['messages']
            self.assertEqual(question['messageId'], ask['messageId'])
            self.cli('reply', str(question['id']), 'finish controlled task')
            done = evidence('done')
            self.assertEqual(done['exit'], 0)
            self.assertTrue(done['stableRetry'])
            [received] = self.cli('wait', '--scope', scope, '--timeout', '5')['messages']
            self.assertEqual(received['messageId'], done['messageId'])
            self.env['WFC_CONTROLLER_TOKEN'] = first['token']
            rejected = self.cli('ack', done['messageId'], success=False)
            self.assertIn('stale_controller_owner', rejected.stderr)
            self.env['WFC_CONTROLLER_TOKEN'] = second['token']
            self.assertEqual(self.cli('ack', done['messageId'])['state'], 'processed')
            view = self.status(dispatch)
            self.assertEqual(view['status'], 'completed')
        self.assertEqual((ask['pid'], done['pid']), (ready['pid'], ready['pid']))
        self.assertEqual(view['attempt'], value['attempt'])
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        print('TAKEOVER ' + json.dumps({'legacy': legacy, 'migrated': bool(migration), 'dispatch': dispatch, 'attempt': value['attempt'],
              'generation': second['generation'], 'ready': ready, 'ask': ask, 'done': done,
              'status': view['status'], 'closed': True}), flush=True)

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

    def test_continue_reuses_owned_execution_and_accepts_new_round_reply(self):
        scope = 'continued-host'
        owner = self.cli('owner', 'acquire', '--scope', scope, '--lease', '180')
        self.env['WFC_CONTROLLER_TOKEN'] = owner['token']
        value = self.dispatch('continue-callback', '--scope', scope)
        dispatch = value['dispatch']
        handle = self.handle(dispatch)
        [question] = self.cli('wait', '--scope', scope, '--timeout', '5')['messages']
        self.cli('reply', str(question['id']), 'finish initial round')
        applied = Path(str(self.evidence) + '.applied')
        self.until(lambda: applied.exists())
        self.env['WFC_DISPATCH'] = dispatch
        self.report.write_text('controlled completed round', encoding='utf-8')
        self.cli('done', '--report', str(self.report), '--attempt', value['attempt'])
        task, report = self.root / 'next task 复验路径-中文-42.md', self.root / 'next report.md'
        task.write_text('controlled next round', encoding='utf-8')
        continued = self.cli('continue', dispatch, '--task', str(task), '--report', str(report))
        self.assertEqual(continued['continued'], dispatch)
        marker = Path(str(self.evidence) + '.continued')
        self.until(lambda: marker.exists())
        status = self.status(dispatch)
        self.assertEqual(status['round'], 2)
        self.assertNotEqual(status['attempt'], value['attempt'])
        self.assertIn(str(task.resolve()), marker.read_text(encoding='utf-8'))
        self.assertIn(status['attempt'], marker.read_text(encoding='utf-8'))
        # Compare every received character against the unchanged producer's full brief.
        import hashlib
        import router
        import zipfile
        from unittest.mock import patch
        callback_program = self.program if zipfile.is_zipfile(self.program) else ROOT / 'router.py'
        import re
        received_brief = marker.read_text(encoding='utf-8')
        ref = re.search(r'--callback-ref ([0-9a-f]{64})', received_brief).group(1)
        with patch.dict(os.environ, {'WFC_STATE': self.env['WFC_STATE']}), patch.object(router, 'PROGRAM', callback_program):
            expected_brief = router.worker_brief(dispatch, task.resolve(), report.resolve(),
                                                opening='[router] New task for you', attempt=status['attempt'], callback_ref=ref)
        self.assertEqual(received_brief, expected_brief)
        brief_digest = hashlib.sha256(received_brief.encode('utf-8')).hexdigest()
        self.assertEqual(brief_digest, hashlib.sha256(expected_brief.encode('utf-8')).hexdigest())
        second = self.handle(dispatch)
        self.assertEqual((second['generation'], second['service'], second['kernel']),
                         (handle['generation'], handle['service'], handle['kernel']))
        [question] = [m for m in self.cli('wait', '--scope', scope, '--timeout', '5')['messages']
                      if m.get('question') == 'controlled next-round question']
        self.assertEqual(question['attempt'], status['attempt'])
        long_reply = ('复验路径-中文-42 Ω😀 ' + 'xyz-123456789 ' * 140 +
                      '\x1b[31mCSI-ORDER\x1b[0m ATTEMPT-TAIL:' + status['attempt'])
        self.cli('reply', str(question['id']), long_reply)
        self.until(lambda: applied.read_text() == 'once\nonce\n')
        recorded = [json.loads(line) for line in Path(str(self.evidence) + '.input.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(recorded[-1], f"[router] Answer to question {question['id']}: {long_reply}")
        import bundled_runtime
        self.until(lambda: 'UNICODE_RECEIVED:复验路径-中文-42 Ω😀' in
                   bundled_runtime.call(handle, 'read').get('data', {}).get('text', ''))
        print('UNICODE_ROUNDTRIP ' + json.dumps({'marker': '复验路径-中文-42 Ω😀',
              'utf8': '复验路径-中文-42 Ω😀'.encode('utf-8').hex(),
              'canonicalTaskExact': True, 'receivedReplyExact': True, 'terminalOutputExact': True,
              'completeBriefChars': len(received_brief), 'completeBriefSHA256': brief_digest, 'attemptExact': True,
              'longReplyChars': len(long_reply), 'longReplySHA256': hashlib.sha256(long_reply.encode('utf-8')).hexdigest(),
              'vtOrderingExact': True}), flush=True)
        replies = [m for m in self.status(dispatch)['messages'] if m['type'] == 'reply']
        self.assertEqual(len(replies), 2)
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')

    def aborted_create_cleanup(self, *extra):
        import bundled_runtime
        import signal
        import time
        import wfc_platform
        # Gate only native kernel startup, then exec the real companion in the same
        # contained PID. No fake backend response or database mutation is involved.
        self.config('record-only')
        self.owner_args = (['--owner-token', extra[extra.index('--owner-token') + 1]]
                           if '--owner-token' in extra else [])
        ready, release = self.root / 'kernel-ready', self.root / 'kernel-release'
        real_kernel = str(Path(os.environ['WFC_BUNDLED_TEST_KERNEL']).resolve())
        gate = self.root / 'kernel-gate'
        gate.write_text('#!' + sys.executable + '\n' +
                        'import os,time\nfrom pathlib import Path\n' +
                        'Path(os.environ["WFC_TEST_KERNEL_READY"]).touch()\n' +
                        'deadline=time.monotonic()+5\n' +
                        'while not Path(os.environ["WFC_TEST_KERNEL_RELEASE"]).exists():\n' +
                        '    if time.monotonic()>deadline: raise TimeoutError("controlled kernel gate expired")\n' +
                        '    time.sleep(.01)\n' +
                        'os.execv(os.environ["WFC_TEST_REAL_KERNEL"], [os.environ["WFC_TEST_REAL_KERNEL"]])\n', encoding='utf-8')
        gate.chmod(0o755)
        self.env.update(WFC_BUNDLED_KERNEL=str(gate), WFC_TEST_REAL_KERNEL=real_kernel,
                        WFC_TEST_KERNEL_READY=str(ready), WFC_TEST_KERNEL_RELEASE=str(release))
        process = subprocess.Popen([sys.executable, str(self.program), 'dispatch', '--backend', 'bundled',
                                    '--host', 'codex', '--task', str(self.task), '--cwd', str(self.root),
                                    '--report', str(self.report), '--launch-config', str(self.launch), *extra],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        creation = wfc_platform.process_identity(process.pid)
        try:
            self.until(lambda: ready.exists(), timeout=3)
            [view] = self.cli('status')['dispatches']
            dispatch = view['dispatch']
            self.dispatches.append(dispatch)
            [create] = [item for item in view['audit'] if item['operation'] == 'create']
            self.assertEqual(create['result'], 'pending')
            self.assertIsNone(process.poll())
            self.assertEqual(wfc_platform.process_identity(process.pid), creation)
            process.send_signal(signal.SIGINT)
            process.communicate(timeout=3)
            self.assertNotEqual(process.returncode, 0)
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
            process.communicate()
        handle = view['backendHandle']
        def active():
            try:
                value = bundled_runtime.read_json(handle['snapshot'])
                return value if value.get('state') == 'active' else None
            except (OSError, ValueError):
                return None
        snapshot = self.until(active)
        self.identities.extend(snapshot[key] for key in ('service', 'kernel', 'guardian'))
        self.until(lambda: any(item['type'] == 'question' for item in self.status(dispatch)['messages']))
        self.assertEqual(bundled_runtime.call(snapshot, 'close')['result'], 'completed')
        receipt = bundled_runtime.read_json(handle['snapshot'])
        self.assertTrue(bundled_runtime.confirmed_cleanup(handle, receipt))
        stopped = self.status(dispatch)
        self.assertFalse(stopped['backendAlive'])
        self.assertEqual(next(a for a in stopped['audit'] if a['id'] == create['id'])['result'], 'pending')
        self.assertEqual(next(m for m in stopped['messages'] if m['type'] == 'question')['state'], 'unsent')
        blocked = self.cli('close', dispatch, *self.owner_args, success=False)
        self.assertIn('lifecycle_reconcile_required', blocked.stderr)
        print('ABORTED_CREATE_SCENE ' + json.dumps({'create': 'pending', 'backendAlive': False,
              'cleanupReceiptAuthenticated': True, 'question': 'unsent', 'inputReplayed': False}), flush=True)
        return dispatch, create, handle, receipt

    def cleanup_reconcile_args(self, dispatch, audit):
        return ('reconcile', dispatch, str(audit), '--result', 'unknown', '--actor', 'controlled-operator',
                '--evidence', 'owned aborted create cleanup', '--cleanup-receipt')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'controlled foreground SIGINT reproduction')
    def test_aborted_create_cleanup_receipt_has_public_recovery(self):
        dispatch, create, handle, receipt = self.aborted_create_cleanup()
        args = self.cleanup_reconcile_args(dispatch, create['id'])
        recovered = self.cli(*args)
        self.assertEqual(recovered['result'], 'unknown')
        self.assertFalse(recovered['backendAlive'])
        self.assertFalse(recovered['inputReplayed'])
        self.assertEqual(recovered['cleanupReceipt']['proves'], 'execution-empty')
        self.assertEqual(recovered['cleanupReceipt']['attempt'], create['attempt'])
        self.assertEqual(recovered['cleanupReceipt']['generation'], handle['generation'])
        self.assertEqual(len(recovered['cleanupReceipt']['receiptSha256']), 64)
        self.assertNotIn('token', json.dumps(recovered))
        duplicate = self.cli(*args)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(duplicate['cleanupReceipt'], recovered['cleanupReceipt'])
        current = self.status(dispatch)
        audit = next(a for a in current['audit'] if a['id'] == create['id'])
        self.assertEqual(audit['result'], 'unknown')
        self.assertEqual(json.loads(audit['resolution']['evidence'])['cleanupReceipt'], recovered['cleanupReceipt'])
        self.assertFalse(any(m['type'] in ('reply', 'done') for m in current['messages']))
        self.assertFalse(Path(str(self.evidence) + '.applied').exists())
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        resumed = self.cli('resume', dispatch)
        current_handle = self.handle(dispatch)
        self.assertNotEqual(resumed['attempt'], create['attempt'])
        self.assertNotEqual(current_handle['generation'], handle['generation'])
        stale = self.cli(*args, success=False)
        self.assertIn('cleanup_receipt_requires_current_bundled_create_unknown', stale.stderr)
        self.assertTrue(self.status(dispatch)['backendAlive'])
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'controlled foreground SIGINT reproduction')
    def test_cleanup_reconcile_refuses_insufficient_wrong_or_stale_receipts(self):
        import hashlib
        import hmac
        import bundled_runtime as runtime
        dispatch, create, handle, receipt = self.aborted_create_cleanup()
        args = self.cleanup_reconcile_args(dispatch, create['id'])
        def signed(**changes):
            value = {**receipt, **changes}
            payload = {key: item for key, item in value.items() if key not in ('token', 'receiptSignature')}
            value['receiptSignature'] = hmac.new(value['token'].encode(), json.dumps(
                payload, sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
            return value
        unsigned = dict(receipt); unsigned.pop('receiptSignature')
        tampered = dict(receipt); tampered['final_screen'] = 'controlled modified receipt'
        variants = [('missing', None), ('non-object', []), ('unsigned', unsigned), ('tampered', tampered),
                    ('not-confirmed', signed(cleanup='unknown')), ('not-terminal', signed(state='active'))]
        for key in ('unit', 'attempt', 'generation', 'nonce'):
            variants.append((key, signed(**{key: 'controlled-other'})))
        for role in ('service', 'kernel', 'guardian', 'containment'):
            # A pre-hello durable handle may not yet record every role. Known
            # bindings must match even under a valid signature; other body changes
            # still require the original authenticated receipt, never a trace.
            bad = signed(**{role: {'controlled': 'other'}}) if handle.get(role) else {**receipt, role: {'controlled': 'other'}}
            variants.append((role, bad))
        variants.append(('key substitution', signed(token='controlled-other-key',
                         capabilityHash=hashlib.sha256(b'controlled-other-key').hexdigest())))
        try:
            for label, bad in variants:
                with self.subTest(receipt=label):
                    if bad is None: Path(handle['snapshot']).unlink(missing_ok=True)
                    else: runtime.publish(handle['snapshot'], bad)
                    refused = self.cli(*args, success=False)
                    self.assertNotEqual(refused.returncode, 0)
                    self.assertIn('bundled_cleanup_unconfirmed', refused.stderr)
                    self.assertEqual(next(a for a in self.status(dispatch)['audit'] if a['id'] == create['id'])['result'], 'pending')
        finally:
            runtime.publish(handle['snapshot'], receipt)
        for wrong_result in ('accepted', 'definitely-not-sent', 'refused'):
            wrong = list(args); wrong[wrong.index('unknown')] = wrong_result
            refused = self.cli(*wrong, success=False)
            self.assertIn('cleanup_receipt_requires_current_bundled_create_unknown', refused.stderr)
        wrong_audit = list(args); wrong_audit[2] = str(create['id'] + 1000)
        self.assertIn('cleanup_receipt_requires_current_bundled_create_unknown', self.cli(*wrong_audit, success=False).stderr)
        arbitrary_path = self.cli(*args, str(self.root / 'untrusted.json'), success=False)
        self.assertNotEqual(arbitrary_path.returncode, 0)
        self.assertEqual(self.cli(*args)['result'], 'unknown')
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'controlled foreground SIGINT reproduction')
    def test_cleanup_reconcile_concurrent_duplicate_and_conflict_are_audited_once(self):
        dispatch, create, _, _ = self.aborted_create_cleanup()
        args = self.cleanup_reconcile_args(dispatch, create['id'])
        processes = [subprocess.Popen([sys.executable, str(self.program), *args], env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            outcomes = []
            for process in processes:
                output, error = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, error)
                outcomes.append(json.loads(output))
            self.assertEqual(sorted(value['duplicate'] for value in outcomes), [False, True])
            self.assertEqual(outcomes[0]['cleanupReceipt'], outcomes[1]['cleanupReceipt'])
            conflict = list(args); conflict[conflict.index('controlled-operator')] = 'other-operator'
            self.assertIn('already unknown', self.cli(*conflict, success=False).stderr)
            self.assertEqual(len([a for a in self.status(dispatch)['audit'] if a['id'] == create['id']]), 1)
            self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        finally:
            for process in processes:
                if process.poll() is None: process.kill()
                process.communicate()

    @unittest.skipUnless(sys.platform.startswith('linux'), 'controlled FIFO/owner takeover regression')
    def test_cleanup_receipt_io_has_no_database_writer_and_rechecks_owner_after_takeover(self):
        import errno
        import time
        import bundled_runtime as runtime
        owner = self.cli('owner', 'acquire', '--scope', 'cleanup-fixture', '--owner', 'first')
        dispatch, create, handle, receipt = self.aborted_create_cleanup(
            '--scope', 'cleanup-fixture', '--owner-token', owner['token'])
        self.owner_args = ['--owner-token', owner['token']]
        args = self.cleanup_reconcile_args(dispatch, create['id'])
        other = self.cli('owner', 'acquire', '--scope', 'other-fixture', '--owner', 'other')
        wrong_scope = self.cli(*args, '--owner-token', other['token'], success=False)
        self.assertIn('stale_controller_owner', wrong_scope.stderr)
        path = Path(handle['snapshot'])
        path.unlink(); os.mkfifo(path, 0o600)
        process = subprocess.Popen([sys.executable, str(self.program), *args, *self.owner_args], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        writer = None
        try:
            deadline = time.monotonic() + 5
            while writer is None and time.monotonic() < deadline:
                try: writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as error:
                    if error.errno != errno.ENXIO: raise
                    time.sleep(.01)
            self.assertIsNotNone(writer, 'reconciliation never reached its controlled receipt read')
            # The reader waits on owned file IO. Takeover must still acquire SQLite.
            takeover = subprocess.run([sys.executable, str(self.program), 'owner', 'takeover', '--scope', 'cleanup-fixture',
                                       '--owner', 'next'], env=self.env, capture_output=True, text=True, timeout=2)
            self.assertEqual(takeover.returncode, 0, takeover.stderr)
            current_owner = json.loads(takeover.stdout)
            self.owner_args = ['--owner-token', current_owner['token']]
            payload = json.dumps(receipt).encode('utf-8')
            os.set_blocking(writer, True)
            with os.fdopen(writer, 'wb') as stream: stream.write(payload)
            writer = None
            _, error = process.communicate(timeout=15)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn('stale_controller_owner', error)
        finally:
            if writer is not None: os.close(writer)
            if process.poll() is None: process.kill()
            process.communicate()
            path.unlink(missing_ok=True); runtime.publish(path, receipt)
        stale = self.cli(*args, '--owner-token', owner['token'], success=False)
        self.assertIn('stale_controller_owner', stale.stderr)
        self.assertEqual(next(a for a in self.status(dispatch)['audit'] if a['id'] == create['id'])['result'], 'pending')
        accepted = self.cli(*args, *self.owner_args)
        self.assertEqual(accepted['result'], 'unknown')
        audit = next(a for a in self.status(dispatch)['audit'] if a['id'] == create['id'])
        self.assertEqual(audit['ownerGeneration'], owner['generation'])
        self.assertEqual(audit['resolution']['ownerGeneration'], current_owner['generation'])
        self.assertTrue(self.cli(*args, *self.owner_args)['duplicate'])
        self.assertIn('stale_controller_owner', self.cli(*args, '--owner-token', owner['token'], success=False).stderr)
        self.assertEqual(self.cli('close', dispatch, *self.owner_args)['result'], 'completed')

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
        from bundled_diagnostics import close_evidence, safe_outcome
        interrupted = self.cli('interrupt', dispatch)
        self.assertEqual(interrupted['result'], 'accepted', safe_outcome(interrupted))
        closed = self.cli('close', dispatch)
        self.assertEqual(closed['result'], 'completed', json.dumps({
            'close': safe_outcome(closed), 'evidence': close_evidence(dispatch, first)}))
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
        self.until(lambda: any(item['type'] == 'question' for item in self.status(value['dispatch'])['messages']))
        self.assertEqual(self.status(value['dispatch'])['status'], 'waiting-human')
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


    def question(self):
        messages = self.cli('wait', '--timeout', '5')['messages']
        return next(item for item in messages if item.get('question') == 'controlled question')

    def test_malformed_and_deep_connections_leave_same_execution_live(self):
        import socket
        import struct
        import bundled_runtime as runtime
        value = self.dispatch('record-only')
        handle = self.handle(value['dispatch'])
        malformed = [b'{', b'[]', b'\xff', b'{"a":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}']
        for raw in malformed:
            for authenticated in (False, True):
                with socket.create_connection(('127.0.0.1', handle['endpoint']), timeout=2) as stream:
                    stream.settimeout(2)
                    if authenticated:
                        stream.sendall(runtime.encode({'challenge': 'x' * 32}))
                        runtime.receive(stream)
                        stream.sendall(runtime.encode({'token': handle['token']}))
                    try:
                        stream.sendall(struct.pack('!I', len(raw)) + raw)
                        self.assertEqual(stream.recv(1), b'')
                    except ConnectionResetError:
                        pass
                self.assertEqual(runtime.call(handle, 'read')['result'], 'accepted')
                self.assertEqual(self.status(value['dispatch'])['backendHandle']['generation'], handle['generation'])
        self.assertTrue(self.status(value['dispatch'])['backendAlive'])

    def test_slow_unauthenticated_backlog_cannot_delay_parent_exit_cleanup(self):
        import socket
        import threading
        import time
        import wfc_platform
        value = self.dispatch('tree')
        handle = self.handle(value['dispatch'])
        descendant = self.descendant_identity()
        self.question()
        stopped = threading.Event()
        clients = []
        def drip():
            try:
                for _ in range(8):
                    stream = socket.create_connection(('127.0.0.1', handle['endpoint']), timeout=1)
                    clients.append(stream)
                while not stopped.wait(.04):
                    for stream in clients:
                        try:
                            stream.sendall(b'\x00')
                        except OSError:
                            pass
            except OSError:
                pass
        thread = threading.Thread(target=drip)
        thread.start()
        try:
            # Kill only the identified PTY host, leaving an escaped descendant.
            # Polling must run despite an accept backlog and partial frame progress.
            receipt = json.loads(next((self.root / 'state' / 'bundled').glob('*.launch.json')).read_text())
            pid, creation = receipt['pid'], receipt['processStart']
            self.assertEqual(wfc_platform.process_identity(pid), creation)
            import signal
            started = time.monotonic()
            os.kill(pid, signal.SIGTERM)
            self.assert_stopped(descendant)
            self.assertLess(time.monotonic() - started, 4)
        finally:
            stopped.set()
            for stream in clients:
                stream.close()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.status(value['dispatch'])['backendAlive'])

    @unittest.skipUnless(os.name != 'nt', 'executable script protocol fixture is Unix-only')
    def test_bad_companion_protocol_refuses_worker_launch(self):
        bad = self.root / 'bad-kernel'
        bad.write_text('#!' + sys.executable + '\nimport json,time\nprint(json.dumps({"event":"hello","generation":"bad","protocol":999}),flush=True)\ntime.sleep(30)\n')
        bad.chmod(0o755)
        self.env['WFC_BUNDLED_KERNEL'] = str(bad)
        failed = self.cli('dispatch', '--backend', 'bundled', '--host', 'codex', '--task', str(self.task),
                          '--cwd', str(self.root), '--report', str(self.report), '--launch-config', str(self.launch),
                          success=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn('startup not confirmed', failed.stderr)
        [view] = self.cli('status')['dispatches']
        self.dispatches.append(view['dispatch'])
        self.assertEqual(view['status'], 'unknown')
        self.assertFalse(self.evidence.exists())
        self.assertFalse(self.report.exists())

    def test_owner_and_callback_credentials_remain_isolated(self):
        owner = self.cli('owner', 'acquire', '--scope', 'controlled-owner', '--owner', 'controller')
        self.owner_args = ['--owner-token', owner['token']]
        value = self.dispatch('record-only', '--scope', 'controlled-owner', *self.owner_args)
        self.handle(value['dispatch'])
        question = self.cli('wait', '--timeout', '5', '--scope', 'controlled-owner', *self.owner_args)['messages'][0]
        rejected = self.cli('reply', str(question['id']), 'unsafe', '--owner-token', 'stale', success=False)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn('stale_controller_owner', rejected.stderr)
        self.report.write_text('callback isolation fixture')
        for token, attempt in (('stale', value['attempt']), (owner['token'], 'a-stale')):
            failed = subprocess.run([sys.executable, str(self.program), 'done', '--report', str(self.report),
                                     '--attempt', attempt], capture_output=True, text=True, encoding='utf-8', timeout=15,
                                    env={**self.env, 'WFC_DISPATCH': value['dispatch'], 'WFC_OWNER_TOKEN': token})
            self.assertNotEqual(failed.returncode, 0)
        view = self.status(value['dispatch'])
        self.assertEqual(view['attempt'], value['attempt'])
        self.assertEqual(view['status'], 'waiting-human')
        self.assertFalse(any(item['type'] in ('reply', 'done') for item in view['messages']))
        self.assertFalse(Path(str(self.evidence) + '.applied').exists())

    def test_concurrent_public_reply_claims_apply_one_atomic_answer(self):
        value = self.dispatch('record-only')
        self.handle(value['dispatch'])
        question = self.question()
        processes = [subprocess.Popen([sys.executable, str(self.program), 'reply', str(question['id']), answer],
                                      env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                     for answer in ('concurrent answer A', 'concurrent answer B')]
        try:
            replies = [process.communicate(timeout=20) for process in processes]
            self.assertEqual(sorted(process.returncode for process in processes), [0, 1], replies)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.until(lambda: Path(str(self.evidence) + '.applied').exists())
        self.assertEqual(Path(str(self.evidence) + '.applied').read_text(), 'once\n')
        view = self.status(value['dispatch'])
        [reply] = [item for item in view['messages'] if item['type'] == 'reply']
        [actual] = [json.loads(line) for line in Path(str(self.evidence) + '.input.jsonl').read_text().splitlines()]
        self.assertEqual(actual, f"[router] Answer to question {question['id']}: {reply['answer']}")

    def test_read_only_refusal_releases_unapplied_public_reply(self):
        value = self.dispatch('read-only')
        self.handle(value['dispatch'])
        question = self.question()
        refused = self.cli('reply', str(question['id']), 'unsafe', success=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('paused', refused.stderr)
        self.assertFalse(Path(str(self.evidence) + '.applied').exists())
        self.assertFalse(any(item['type'] == 'reply' for item in self.status(value['dispatch'])['messages']))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux RSS pressure measurement')
    def test_output_pressure_drains_while_large_input_and_query_reply_are_pending(self):
        import hashlib
        import threading
        import time
        import bundled_runtime as runtime
        value = self.dispatch('pressure')
        handle = self.handle(value['dispatch'])
        self.question()
        def rss():
            status = Path('/proc/' + str(handle['kernel']['pid']) + '/status').read_text()
            return int(next(line for line in status.splitlines() if line.startswith('VmRSS:')).split()[1]) * 1024
        initial = peak = rss()
        payload = 'x' * (96 * 1024)
        result = []
        Path(str(self.evidence) + '.pressure-gate').touch()
        thread = threading.Thread(target=lambda: result.append(runtime.call(handle, 'input', text='pressure-input:' + payload + '\r')))
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while thread.is_alive() and time.monotonic() < deadline:
                peak = max(peak, rss())
                time.sleep(.01)
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive(), 'input deadlocked behind output backpressure')
            self.assertEqual(result[0]['result'], 'accepted', result)
            target = Path(str(self.evidence) + '.pressure-input')
            self.until(lambda: target.exists())
            self.assertEqual(target.read_text(), hashlib.sha256(payload.encode()).hexdigest())
            self.assertEqual(int(Path(str(self.evidence) + '.pressure-written').read_text()), 16 * 1024 * 1024)
            self.assertLess(peak - initial, 16 * 1024 * 1024)
            self.assertEqual(runtime.call(handle, 'read')['result'], 'accepted')
            print('PRESSURE_METRICS ' + json.dumps({'output_bytes': 16 * 1024 * 1024,
                  'input_bytes': len(payload), 'rss_initial': initial, 'rss_peak': peak, 'rss_growth': peak - initial}))
        finally:
            thread.join(timeout=7)



    def test_applied_input_with_lost_response_retains_public_reply_claim(self):
        import hashlib
        import hmac
        import socket
        import threading
        import bundled_runtime as runtime
        import wfc_platform
        value = self.dispatch('record-only')
        handle = self.handle(value['dispatch'])
        question = self.question()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        listener.settimeout(.1)
        stopped = threading.Event()
        inputs, failures = [], []
        def serve():
            while not stopped.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    with client:
                        client.settimeout(3)
                        challenge = runtime.receive(client)['challenge']
                        proof = hmac.new(handle['token'].encode(), ('server:' + challenge).encode(), hashlib.sha256).hexdigest()
                        client.sendall(runtime.encode({'proof': proof}))
                        self.assertEqual(runtime.receive(client)['token'], handle['token'])
                        request = runtime.receive(client)
                        result = runtime.call(handle, request['op'], **{
                            key: request[key] for key in ('text', 'cols', 'rows') if key in request})
                        if request['op'] == 'input':
                            inputs.append(request['text'])
                            self.assertEqual(result['result'], 'accepted', result)
                            if request['text'] == '\r':
                                continue  # real PTY write completed; deliberately lose only its response
                        client.sendall(runtime.encode({**result, 'id': request['id'], 'generation': handle['generation']}))
                except Exception as error:
                    failures.append(str(error))
        proxy = threading.Thread(target=serve)
        proxy.start()
        runtime.publish(handle['snapshot'], {**handle, 'endpoint': listener.getsockname()[1]})
        try:
            failed = self.cli('reply', str(question['id']), 'apply once', success=False)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn('uncertain', failed.stderr)
            marker = Path(str(self.evidence) + '.applied')
            self.until(lambda: marker.exists())
            self.assertEqual(marker.read_text(), 'once\n')
            before = self.status(value['dispatch'])
            self.assertEqual(before['status'], 'unknown')
            [claimed] = [message for message in before['messages'] if message['type'] == 'reply']
            self.assertEqual(claimed['to'], question['id'])
            retry = self.cli('reply', str(question['id']), 'apply once', success=False)
            self.assertNotEqual(retry.returncode, 0)
            self.assertIn('already answered', retry.stderr)
            self.assertEqual(marker.read_text(), 'once\n')
            self.assertEqual(inputs, ['\x1b[200~' + f"[router] Answer to question {question['id']}: apply once" + '\x1b[201~', '\r'])
            after = self.status(value['dispatch'])
            self.assertEqual(after['attempt'], value['attempt'])
            self.assertEqual([message['id'] for message in after['messages'] if message['type'] == 'reply'], [claimed['id']])
        finally:
            if wfc_platform.process_identity(handle['service']['pid']) == handle['service']['creation']:
                runtime.publish(handle['snapshot'], handle)
            stopped.set()
            listener.close()
            proxy.join(timeout=8)
        self.assertFalse(proxy.is_alive())
        self.assertFalse(failures, failures)


    def test_completed_close_with_lost_response_uses_matching_receipt_once(self):
        import hashlib
        import hmac
        import socket
        import threading
        import bundled_runtime as runtime
        value = self.dispatch('record-only')
        handle = self.handle(value['dispatch'])
        self.question()
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        listener.settimeout(.1)
        stopped = threading.Event()
        operations, failures = [], []
        def serve():
            while not stopped.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    with client:
                        client.settimeout(7)
                        challenge = runtime.receive(client)['challenge']
                        proof = hmac.new(handle['token'].encode(), ('server:' + challenge).encode(), hashlib.sha256).hexdigest()
                        client.sendall(runtime.encode({'proof': proof}))
                        self.assertEqual(runtime.receive(client)['token'], handle['token'])
                        request = runtime.receive(client)
                        operations.append(request['op'])
                        result = runtime.call(handle, request['op'])
                        if request['op'] == 'close':
                            self.assertEqual(result['result'], 'completed', result)
                            continue  # Real container emptied; lose only the completed response.
                        client.sendall(runtime.encode({**result, 'id': request['id'], 'generation': handle['generation']}))
                except Exception as error:
                    failures.append(type(error).__name__)
        proxy = threading.Thread(target=serve)
        proxy.start()
        runtime.publish(handle['snapshot'], {**handle, 'endpoint': listener.getsockname()[1]})
        try:
            result = self.cli('close', value['dispatch'])
            self.assertEqual(result['result'], 'completed', result)
            receipt = runtime.read_json(handle['snapshot'])
            self.assertTrue(runtime.confirmed_cleanup(handle, receipt))
            self.assertEqual(receipt['state'], 'closed')
            self.assertEqual(operations.count('close'), 1)
        finally:
            stopped.set()
            listener.close()
            proxy.join(timeout=8)
        self.assertFalse(proxy.is_alive())
        self.assertFalse(failures, failures)



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
                    auth = bundled_runtime.receive(stream)
                    if auth.get('token') != handle['token']:
                        raise ValueError('test capability mismatch')
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
