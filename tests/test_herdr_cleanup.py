"""Fixture cleanup control flow; modeled taskkill results are not Windows evidence."""
import contextlib
import ctypes
from ctypes import wintypes
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


class PrefillSynchronization(unittest.TestCase):
    def fixture(self):
        tmp = tempfile.TemporaryDirectory(prefix='controlled prefill ')
        self.addCleanup(tmp.cleanup)
        case = test_herdr.HerdrContract('test_initial_prefill_and_concurrent_nudge_do_not_mix')
        case.root = Path(tmp.name)
        case.host = case.root / 'host.py'
        case.launch = case.root / 'launch.json'
        case.task = case.root / 'task.md'
        case.report = case.root / 'report.md'
        case.endpoint = 'controlled-not-a-service'
        case.env = {**os.environ, 'WFC_STATE': str(case.root / 'state')}
        case.server_job = None
        self.addCleanup(case.doCleanups)
        return case

    def test_failed_startup_still_reaps_an_already_exited_cli(self):
        case = self.fixture()
        child = Mock()
        child.pid = 123
        child.returncode = 0
        child.poll.return_value = 0
        child.communicate.return_value = ('', '')
        case.cli = Mock(side_effect=RuntimeError('controlled startup failure'))
        case.prefill_marker = Mock(side_effect=RuntimeError('controlled startup failure'))
        with patch.object(test_herdr.subprocess, 'Popen', return_value=child):
            with self.assertRaisesRegex(RuntimeError, 'controlled startup failure'):
                case.test_initial_prefill_and_concurrent_nudge_do_not_mix()
            self.assertTrue(case.doCleanups())
        child.communicate.assert_called()
        child.stdout.close.assert_called()
        child.stderr.close.assert_called()

    def test_staged_clients_observe_real_lock_contention_before_release(self):
        case = self.fixture()
        sync, clients, cli = case.prefill_fixture()
        program = case.root / 'controlled_program'
        program.mkdir()
        controlled = program / 'wfc.py'
        controlled.write_text('''import json,router
from prefill_control import mark,wait_for
class owner_lock(router.input_lock):
    def __init__(self,scope):self.path=router.state_dir()/('owner-'+scope+'.lock')
def main(argv):
    router.state_dir().mkdir(parents=True,exist_ok=True)
    with router.input_lock('d-controlled'):
        if argv[0]=='dispatch':
            mark('host-waiting',{'dispatch':'d-controlled'})
            wait_for('draw-composer',8)
        else:
            with owner_lock('controlled'):mark('controlled-nudge-applied',{})
    print(json.dumps({'operation':argv[0]}))
    return 0
''', encoding='utf-8')
        case.env['PYTHONPATH'] = str(test_herdr.ROOT)
        with patch.object(test_herdr, 'PROGRAM', controlled):
            nudge = case.prefill_client(sync, clients, cli, 'nudge')
            case.prefill_marker(sync, 'nudge-ready', clients)
            dispatch = case.prefill_client(sync, clients, cli, 'dispatch')
            case.prefill_marker(sync, 'dispatch-ready', clients)
            case.prefill_release(sync, 'run-dispatch', {'argv': ['dispatch']})
            case.prefill_marker(sync, 'host-waiting', clients)
            held = case.prefill_marker(sync, 'dispatch-acquired', clients)
            case.prefill_release(sync, 'run-nudge', {'argv': ['nudge', 'd-controlled']})
            self.assertEqual(case.prefill_marker(sync, 'nudge-blocked', clients), held)
            self.assertIsNone(dispatch.poll())
            self.assertIsNone(nudge.poll())
            self.assertFalse((sync / 'nudge-acquired').exists())
            case.prefill_release(sync, 'draw-composer', {})
            for child in (dispatch, nudge):
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, err)
                self.assertIn(json.loads(out)['operation'], ('dispatch', 'nudge'))
            case.prefill_marker(sync, 'controlled-nudge-applied', clients)

    def test_aborted_gates_reap_both_staged_clients_and_close_pipes(self):
        case = self.fixture()
        sync, clients, cli = case.prefill_fixture()
        for role in ('nudge', 'dispatch'):
            case.prefill_client(sync, clients, cli, role)
            case.prefill_marker(sync, role + '-ready', clients)
        self.assertTrue(case.doCleanups())
        for child in clients:
            self.assertEqual(child.returncode, 1)
            self.assertTrue(child.stdout.closed)
            self.assertTrue(child.stderr.closed)

    def test_failed_job_cleanup_still_reaps_cli_and_surfaces_both_errors(self):
        case = self.fixture()
        sync = case.root / 'failed-cleanup'
        sync.mkdir()
        child = Mock(pid=123, returncode=1)
        child.poll.return_value = None
        child.communicate.side_effect = [subprocess.TimeoutExpired('controlled', 10), ('', '')]
        case.server_job = Mock()
        case.stop_server = Mock(side_effect=AssertionError('Job did not empty'))
        with self.assertRaisesRegex(AssertionError, 'Job did not empty'):
            case.stop_prefill_clients(sync, [child])
        child.kill.assert_called_once_with()
        self.assertEqual(child.communicate.call_count, 2)
        child.stdout.close.assert_called_once_with()
        child.stderr.close.assert_called_once_with()

    def test_controlled_host_records_blank_prefill_and_one_separate_nudge(self):
        case = self.fixture()
        callback = case.root / 'controlled_callback.py'
        callback.write_text('raise SystemExit(0)\n', encoding='utf-8')
        with patch.object(test_herdr, 'PROGRAM', callback):
            sync, clients, _ = case.prefill_fixture()
        child = subprocess.Popen([sys.executable, '-u', str(case.host), '--prompt', 'controlled --attempt a-test'],
                                 env={**case.env, 'WFC_PREFILL_SYNC': str(sync), 'WFC_DISPATCH': 'd-test'},
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        clients.append(child)
        waiting = case.prefill_marker(sync, 'host-waiting', clients)
        self.assertEqual(waiting['attempt'], 'a-test')
        case.prefill_release(sync, 'draw-composer', {})
        line = '[router] Message from the controller: concurrent input'
        out, err = child.communicate(input='\n' + line + '\n', timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertIn('Build · controlled OpenCode\n', out)
        self.assertEqual(case.prefill_marker(sync, 'host-inputs', clients)['lines'], ['', line])


class OwnedWindowsDescendant:
    """Retain a read-only handle to one creation identity across Job cleanup."""
    def __init__(self, identity):
        self.identity = dict(identity)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'GetProcessTimes': ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'GetExitCodeProcess': ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.OpenProcess(0x1000 | 0x100000, False, identity['pid'])
        if not self.handle:
            raise OSError(ctypes.get_last_error(), 'cannot observe owned descendant')
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in times)):
                raise OSError(ctypes.get_last_error(), 'cannot verify descendant creation')
            creation = 'windows:' + str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            if creation != identity['creation']:
                raise AssertionError('descendant creation identity changed before observation')
            if self.kernel.WaitForSingleObject(self.handle, 0) != 258:
                raise AssertionError('controlled descendant must be live before cleanup')
        except BaseException:
            self.close()
            raise

    def wait_exited(self):
        # Wait for this exact process object's exit event; Job accounting and a
        # creation value alone are not substitutes for the native signal.
        observed = self.kernel.WaitForSingleObject(self.handle, 5000)
        if observed != 0:
            raise AssertionError('owned descendant exit signal unconfirmed: ' + json.dumps({
                **self.identity, 'wait_result': observed,
                'winerror': ctypes.get_last_error() if observed == 0xffffffff else None}))
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise OSError(ctypes.get_last_error(), 'cannot read signaled descendant exit code')
        return {**self.identity, 'state': 'exited', 'wait_result': observed, 'exit_code': code.value}

    def close(self):
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise OSError(ctypes.get_last_error(), 'cannot close descendant observation handle')
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *error):
        self.close()


class DescendantObservation(unittest.TestCase):
    def kernel(self, waits, exit_value=1):
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.side_effect = waits
        def times(handle, created, exited, system, user):
            ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents.dwLowDateTime = 7
            return True
        def exit_code(handle, code):
            ctypes.cast(code, ctypes.POINTER(wintypes.DWORD)).contents.value = exit_value
            return True
        kernel.GetProcessTimes.side_effect = times
        kernel.GetExitCodeProcess.side_effect = exit_code
        return kernel

    def test_native_case_uses_exit_signal_when_creation_identity_is_retained(self):
        import wfc_platform
        kernel = self.kernel([258, 0])
        identity = {'pid': 456, 'creation': 'windows:7'}
        fixture = SimpleNamespace(server=Mock(returncode=0), log=io.StringIO(json.dumps(identity)),
                                  stop_server=Mock())
        fixture.server.wait.return_value = 0
        case = HerdrCleanup('test_native_job_cleans_descendant_after_root_exit')
        native = HerdrCleanup.test_native_job_cleans_descendant_after_root_exit
        body = getattr(native, '__wrapped__', native)
        with patch.object(case, 'launch', return_value=fixture), \
                patch.object(wfc_platform, 'process_identity', return_value=identity['creation']), \
                patch.object(wfc_platform, 'process_probe', side_effect=[
                    {'state': 'alive', 'identity': identity['creation']},
                    {'state': 'exited', 'identity': None}]), \
                patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            body(case)
        fixture.stop_server.assert_called_once_with()
        kernel.WaitForSingleObject.assert_any_call(123, 5000)

    def test_unsignaled_or_failed_wait_cannot_confirm_exit(self):
        for observed in (258, 0xffffffff):
            with self.subTest(wait_result=observed):
                kernel = self.kernel([258, observed])
                with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                        patch.object(ctypes, 'get_last_error', return_value=5, create=True):
                    with self.assertRaisesRegex(AssertionError, 'exit signal unconfirmed'):
                        with OwnedWindowsDescendant({'pid': 456, 'creation': 'windows:7'}) as child:
                            child.wait_exited()
                kernel.GetExitCodeProcess.assert_not_called()
                kernel.CloseHandle.assert_called_once_with(123)

    def test_creation_mismatch_refuses_observation_and_closes_handle(self):
        kernel = self.kernel([])
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            with self.assertRaisesRegex(AssertionError, 'creation identity changed'):
                OwnedWindowsDescendant({'pid': 456, 'creation': 'windows:8'})
        kernel.OpenProcess.assert_called_once_with(0x1000 | 0x100000, False, 456)
        kernel.WaitForSingleObject.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_inaccessible_descendant_is_not_classified_as_exited(self):
        kernel = self.kernel([])
        kernel.OpenProcess.return_value = 0
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                patch.object(ctypes, 'get_last_error', return_value=5, create=True):
            with self.assertRaises(OSError):
                OwnedWindowsDescendant({'pid': 456, 'creation': 'windows:7'})
        kernel.WaitForSingleObject.assert_not_called()

    def test_exit_code_alone_does_not_replace_the_signal(self):
        kernel = self.kernel([258, 0], exit_value=259)
        with patch.object(ctypes, 'WinDLL', return_value=kernel, create=True):
            with OwnedWindowsDescendant({'pid': 456, 'creation': 'windows:7'}) as child:
                # A signaled process can itself return 259; the event is authoritative.
                result = child.wait_exited()
        self.assertEqual(result['state'], 'exited')
        self.assertEqual(result['exit_code'], 259)
        self.assertEqual(result['wait_result'], 0)


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
        before = wfc_platform.process_probe(identity['pid'])
        self.assertEqual(before['state'], 'alive', before)
        self.assertEqual(before['identity'], identity['creation'])
        with OwnedWindowsDescendant(identity) as descendant:
            signaled = None
            try:
                case.stop_server()  # Still requires Job ActiveProcesses == 0 and root reaping.
                self.assertEqual(case.server.returncode, 0)
                signaled = descendant.wait_exited()
            finally:
                after = wfc_platform.process_probe(identity['pid'])
                print('[herdr-descendant-exit] ' + json.dumps({
                    'owned': identity, 'before': before, 'signal': signaled, 'after': after}), file=sys.stderr)
            self.assertEqual(signaled['state'], 'exited')
            self.assertEqual(after['state'], 'exited', after)


if __name__ == '__main__':
    unittest.main()
