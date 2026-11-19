"""One-shot Windows runner probe for the pinned bundled ZIP."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import unittest

if sys.argv[1:2] == ['--debug-dispatch']:
    program, marker, *args = sys.argv[2:]
    def gate(frame, event, arg):
        if (event == 'line' and frame.f_code.co_filename.replace('\\\\', '/').endswith('/bundled_backend.py')
                and frame.f_code.co_name == 'create' and frame.f_lineno == 164):
            Path(marker).write_text(str(os.getpid()))
            time.sleep(45)
            raise TimeoutError('dispatch breakpoint expired')
        return gate

    sys.argv = [program, *args]
    sys.settrace(gate)
    runpy.run_path(program, run_name='__main__')
    sys.exit('unexpected return')

from test_bundled import BundledNative
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bundled_runtime as runtime
import wfc_platform


class WindowsRecovery(BundledNative):
    def test_recovery(self):
        self.config('record-only')
        marker = self.root / 'paused'
        cmd = [sys.executable, __file__, '--debug-dispatch', str(self.program), str(marker),
               'dispatch', '--backend', 'bundled', '--host', 'codex', '--task', str(self.task),
               '--cwd', str(self.root), '--report', str(self.report), '--launch-config', str(self.launch)]
        with subprocess.Popen(cmd, env=self.env) as front:
            try:
                identity = wfc_platform.process_identity(front.pid)
                self.assertTrue(identity)
                self.until(marker.exists, timeout=20)
                [view] = self.cli('status')['dispatches']
                dispatch = view['dispatch']
                self.dispatches.append(dispatch)
                [create] = [a for a in view['audit'] if a['operation'] == 'create']
                self.assertEqual(create['result'], 'pending')
                self.assertEqual(wfc_platform.process_identity(front.pid), identity)
                self.assertIsNone(front.poll())
                front.terminate()
                self.assertNotEqual(front.wait(timeout=5), 0)
            finally:
                if front.poll() is None:
                    front.kill()

        handle = view['backendHandle']
        self.until(lambda: any(m['type'] == 'question' for m in self.status(dispatch)['messages']))
        snap = runtime.read_json(handle['snapshot'])
        self.identities.extend(snap[k] for k in ('service', 'kernel', 'guardian'))
        self.assertEqual(runtime.call(snap, 'close')['result'], 'completed')
        self.until(lambda: runtime.confirmed_cleanup(handle, runtime.read_json(handle['snapshot'])))
        before = self.status(dispatch)
        self.assertFalse(before['backendAlive'])
        [question] = [m for m in before['messages'] if m['type'] == 'question']
        self.assertEqual(question['state'], 'unsent')
        self.assertIn('lifecycle_reconcile_required', self.cli('close', dispatch, success=False).stderr)
        args = self.cleanup_reconcile_args(dispatch, create['id'])
        first = self.cli(*args)
        proof = first['cleanupReceipt']
        self.assertEqual((first['result'], first['inputReplayed'], first['backendAlive']), ('unknown', False, False))
        self.assertEqual((proof['attempt'], proof['generation'], proof['proves']),
                         (create['attempt'], handle['generation'], 'execution-empty'))
        repeat = self.cli(*args)
        self.assertTrue(repeat['duplicate'])
        self.assertEqual(repeat['cleanupReceipt'], proof)
        self.assertEqual(self.cli('close', dispatch)['result'], 'completed')
        after = self.status(dispatch)
        self.assertFalse(after['backendAlive'])
        self.assertEqual(after['attempt'], create['attempt'])
        self.assertEqual(next(a for a in after['audit'] if a['id'] == create['id'])['result'], 'unknown')
        self.assertFalse(any(m['type'] in ('done', 'reply') for m in after['messages']))
        self.assertFalse(Path(str(self.evidence) + '.applied').exists())
        print(json.dumps(dict(dispatch=dispatch, attempt=create['attempt'], create='unknown',
                              cleanup=proof['receiptSha256'], duplicate=True, inputReplayed=False, close='completed')))


if __name__ == '__main__':
    unittest.main(argv=[sys.argv[0], 'WindowsRecovery.test_recovery'], verbosity=2)
