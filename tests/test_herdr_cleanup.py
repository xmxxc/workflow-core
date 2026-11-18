"""Fixture cleanup control flow; modeled taskkill results are not Windows evidence."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_herdr


class HerdrCleanup(unittest.TestCase):
    def launch(self, target, job):
        case = test_herdr.HerdrContract('test_service_restart_stays_unknown_without_replaying_input')
        case.env = dict(os.environ)
        case.log = tempfile.TemporaryFile(mode='w+', encoding='utf-8')
        self.addCleanup(case.log.close)
        self.addCleanup(case.stop_server)
        original_popen = subprocess.Popen
        def popen(command, **kwargs):
            # Run the actual launch gate, substituting only its Herdr command
            # with a bounded controlled Python target. No real Herdr is started.
            self.assertEqual(command[-1], 'server')
            return original_popen([*command[:3], sys.executable, '-c', target], **kwargs)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(test_herdr.os, 'name', 'nt'))
            stack.enter_context(patch.object(test_herdr.subprocess, 'Popen', side_effect=popen))
            if job is not None:
                stack.enter_context(patch.dict(sys.modules, {'bundled_runtime': SimpleNamespace(WindowsContainment=lambda: job)}))
            self.case = case
            case.start_server()
        return case

    def child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stdin.buffer.read(1)'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdin.close()
        self.addCleanup(cleanup)
        return child

    def stop(self, child, taskkill):
        case = test_herdr.HerdrContract('test_service_restart_stays_unknown_without_replaying_input')
        case.server = child
        self.output = io.StringIO()
        with patch.object(test_herdr.os, 'name', 'nt'), \
                patch.object(test_herdr.subprocess, 'run', side_effect=taskkill), \
                contextlib.redirect_stderr(self.output):
            case.stop_server()

    def evidence(self):
        return json.loads(self.output.getvalue().split('[herdr-fixture-stop] ', 1)[1])

    def test_raced_root_exit_records_reaped_handle_but_does_not_prove_tree_cleanup(self):
        child = self.child()
        def taskkill(command, **kwargs):
            child.stdin.write(b'x')
            child.stdin.flush()
            child.wait(timeout=5)
            raise subprocess.CalledProcessError(128, command, output=b'kill stdout', stderr=b'process not found')
        with self.assertRaises(AssertionError) as failure:
            self.stop(child, taskkill)
        evidence = self.evidence()
        self.assertEqual(evidence['poll_before'], None)
        self.assertEqual(evidence['reaped_returncode'], 0)
        self.assertEqual(evidence['taskkill']['returncode'], 128)
        self.assertEqual(evidence['taskkill']['stdout'], "b'kill stdout'")
        self.assertIn('process not found', evidence['taskkill']['stderr'])
        self.assertIsInstance(failure.exception.__cause__, subprocess.CalledProcessError)

    def test_live_root_kill_failure_stays_failed_with_command_output(self):
        child = self.child()
        def taskkill(command, **kwargs):
            raise subprocess.CalledProcessError(128, command, output=b'partial output', stderr=b'access denied')
        with self.assertRaisesRegex(AssertionError, 'tree cleanup unconfirmed'):
            self.stop(child, taskkill)
        evidence = self.evidence()
        self.assertIsNone(child.poll())
        self.assertIsNone(evidence['reaped_returncode'])
        self.assertTrue(evidence['wait_timed_out'])
        self.assertEqual(evidence['taskkill']['returncode'], 128)
        self.assertIn('access denied', evidence['taskkill']['stderr'])

    def test_partial_taskkill_255_stays_failed_without_job_proof(self):
        child = self.child()
        def taskkill(command, **kwargs):
            child.terminate()
            child.wait(timeout=5)
            raise subprocess.CalledProcessError(255, command, output=b'root and descendant terminated',
                                                stderr=b'intermediate process already gone')
        with self.assertRaisesRegex(AssertionError, 'tree cleanup unconfirmed'):
            self.stop(child, taskkill)
        evidence = self.evidence()
        self.assertEqual(evidence['taskkill']['returncode'], 255)
        self.assertIsNotNone(evidence['reaped_returncode'])
        self.assertNotIn('job_empty', evidence)

    def test_already_exited_owned_handle_never_targets_its_old_pid(self):
        child = self.child()
        child.stdin.write(b'x')
        child.stdin.flush()
        child.wait(timeout=5)
        def taskkill(command, **kwargs):
            self.fail('already exited owned process must not be targeted by PID')
        self.stop(child, taskkill)
        evidence = self.evidence()
        self.assertEqual(evidence['poll_before'], 0)
        self.assertEqual(evidence['reaped_returncode'], 0)
        self.assertIsNone(evidence['taskkill'])

    def test_successful_modeled_tree_kill_keeps_flags_and_reaps_owned_handle(self):
        child = self.child()
        def taskkill(command, **kwargs):
            self.assertEqual(command, ['taskkill', '/PID', str(child.pid), '/T', '/F'])
            self.assertEqual(kwargs, dict(capture_output=True, timeout=10, check=True))
            child.terminate()
            return subprocess.CompletedProcess(command, 0, stdout=b'tree stopped', stderr=b'')
        self.stop(child, taskkill)
        evidence = self.evidence()
        self.assertIsNotNone(child.returncode)
        self.assertEqual(evidence['reaped_returncode'], child.returncode)
        self.assertEqual(evidence['taskkill']['returncode'], 0)
        self.assertEqual(evidence['taskkill']['stdout'], "b'tree stopped'")

    def test_taskkill_timeout_preserves_output_and_remains_a_failure(self):
        child = self.child()
        def taskkill(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 10, output=b'partial kill', stderr=b'stalled')
        with self.assertRaises(AssertionError) as failure:
            self.stop(child, taskkill)
        evidence = self.evidence()
        self.assertIsNone(child.poll())
        self.assertTrue(evidence['taskkill']['timed_out'])
        self.assertTrue(evidence['wait_timed_out'])
        self.assertIn('partial kill', evidence['taskkill']['stdout'])
        self.assertIsInstance(failure.exception.__cause__, subprocess.TimeoutExpired)

    def test_owned_job_is_emptied_even_after_root_already_exited(self):
        child = self.child()
        child.stdin.write(b'x')
        child.stdin.flush()
        child.wait(timeout=5)
        case = test_herdr.HerdrContract('test_service_restart_stays_unknown_without_replaying_input')
        case.server = child
        job = case.server_job = Mock()
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            case.stop_server()
        job.stop.assert_called_once_with()
        job.dispose.assert_called_once_with()
        evidence = json.loads(output.getvalue().split('[herdr-fixture-stop] ', 1)[1])
        self.assertTrue(evidence['job_empty'])
        self.assertEqual(evidence['reaped_returncode'], 0)
        self.assertIsNone(evidence['taskkill'])

    def test_launch_gate_blocks_target_until_job_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'started'
            job = Mock()
            def attach(pid):
                self.assertEqual(pid, self.case.server.pid)
                self.assertIsNone(self.case.server.poll())
                self.assertFalse(marker.exists())
            job.attach.side_effect = attach
            case = self.launch('from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("started")', job)
            self.assertEqual(case.server.wait(timeout=5), 0)
            self.assertEqual(marker.read_text(), 'started')
            job.attach.assert_called_once_with(case.server.pid)

    def test_assignment_failure_never_releases_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'must not start'
            job = Mock()
            job.attach.side_effect = OSError('nested Job assignment refused')
            with self.assertRaisesRegex(OSError, 'assignment refused'):
                self.launch('from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("unsafe")', job)
            self.assertFalse(marker.exists())
            self.assertEqual(self.case.server.returncode, 74)
            job.dispose.assert_called_once_with()

    def test_job_stop_failure_is_not_hidden_by_root_reaping(self):
        child = self.child()
        case = test_herdr.HerdrContract('test_service_restart_stays_unknown_without_replaying_input')
        case.server = child
        job = case.server_job = Mock()
        job.stop.side_effect = TimeoutError('Job still populated')
        job.dispose.side_effect = child.terminate
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(AssertionError) as failure:
            case.stop_server()
        evidence = json.loads(output.getvalue().split('[herdr-fixture-stop] ', 1)[1])
        self.assertFalse(evidence['job_empty'])
        self.assertIsNotNone(evidence['reaped_returncode'])
        self.assertIsInstance(failure.exception.__cause__, TimeoutError)

    @unittest.skipUnless(os.name == 'nt', 'requires real Windows Job nesting and process handles')
    def test_native_job_cleans_descendant_after_root_exit(self):
        import wfc_platform
        target = ('import json,subprocess,sys,wfc_platform; '
                  'child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                  'print(json.dumps({"pid":child.pid,"creation":wfc_platform.process_identity(child.pid)}),flush=True)')
        case = self.launch(target, None)  # Uses the actual bundled_runtime.WindowsContainment.
        self.assertEqual(case.server.wait(timeout=5), 0)
        case.log.seek(0)
        identity = json.loads(case.log.read())
        self.assertTrue(identity['creation'])
        self.assertEqual(wfc_platform.process_identity(identity['pid']), identity['creation'])
        case.stop_server()  # ActiveProcesses == 0 is required even though root already exited.
        self.assertNotEqual(wfc_platform.process_identity(identity['pid']), identity['creation'])


if __name__ == '__main__':
    unittest.main()
