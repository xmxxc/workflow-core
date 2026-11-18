"""Fixture cleanup control flow; modeled taskkill results are not Windows evidence."""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_herdr


class HerdrCleanup(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
