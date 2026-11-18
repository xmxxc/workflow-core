"""Safety/fixture checks for the issue #35 diagnostic tool, not a console regression test."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import socket
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location('console_probe', ROOT / 'tools/windows_console_probe.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class ProbeSafety(unittest.TestCase):
    def test_observer_verdict_requires_exact_visible_window_and_creation_identity(self):
        owner = dict(pid=123, creation='windows:1', image='conhost.exe')
        control = dict(event='positive_control', hwnd=456, owner=owner)
        observed = dict(event='console_association', hwnd=456, owner=owner, visible=True, attached_owned_pid_present=True)
        self.assertTrue(probe.positive_control_seen([control, observed]))
        for change in (dict(hwnd=457), dict(visible=False), dict(attached_owned_pid_present=False),
                       dict(owner={**owner, 'creation': 'windows:2'})):
            bad = {**observed, **change}
            self.assertFalse(probe.positive_control_seen([control, bad]))
        self.assertFalse(probe.positive_control_seen([]))
        self.assertFalse(probe.positive_control_seen([control]))

    def test_watchdog_stops_real_owned_child_despite_blocked_sampler(self):
        # Deliberately block this thread. The independent watchdog must still kill its child.
        for trigger in ('STOP', 'deadline'):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory(prefix='wfc35-') as directory:
                root = Path(directory)
                owned = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
                sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
                timer = None
                finished, watcher, outcome = probe.start_watchdog(root / 'STOP',
                    time.monotonic() + (.12 if trigger == 'deadline' else 2), owned.kill)
                try:
                    if trigger == 'STOP':
                        timer = threading.Timer(.06, lambda: (root / 'STOP').touch())
                        timer.start()
                    time.sleep(.3)  # represents a stuck window sampler or RPC
                    self.assertIsNotNone(owned.poll())
                    self.assertIsNone(sentinel.poll())
                    self.assertEqual(outcome, [trigger])
                finally:
                    finished.set()
                    watcher.join(timeout=1)
                    if timer:
                        timer.join(timeout=1)
                    for child in (owned, sentinel):
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=1)

    def test_normal_completion_cancels_watchdog(self):
        with tempfile.TemporaryDirectory(prefix='wfc35-') as directory:
            called = []
            finished, watcher, outcome = probe.start_watchdog(Path(directory) / 'STOP', time.monotonic() + .1,
                                                              lambda: called.append(True))
            finished.set()
            watcher.join(timeout=1)
            self.assertEqual((called, outcome), ([], []))
            self.assertFalse(watcher.is_alive())

    def test_environment_drops_inherited_credentials_and_state(self):
        from unittest.mock import patch
        root = Path('/scratch/fixture')
        with patch.dict(os.environ, dict(OPENAI_API_KEY='SECRET', WFC_STATE='/user/state',
            HERDR_SOCKET_PATH='user-service', HERDR_CONFIG_PATH='/user/config', CODEX_THREAD_ID='user-thread')):
            env = probe.isolated_env(root)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('CODEX_THREAD_ID', env)
        self.assertNotIn('CODEX_HOME', env)
        self.assertNotEqual(env['HERDR_SOCKET_PATH'], 'user-service')
        self.assertEqual(env['HERDR_SOCKET_PATH'], env['WFC_HERDR_ENDPOINT'])
        self.assertEqual(env['HERDR_CONFIG_PATH'], str(root / 'herdr.toml'))
        self.assertEqual(env['WFC_STATE'], str(root / 'state'))
        for kind in ('CONFIG', 'STATE', 'DATA', 'CACHE'):
            self.assertEqual(Path(env['XDG_' + kind + '_HOME']).parent, root)

    @unittest.skipIf(os.name == 'nt', 'Linux refusal contract')
    def test_linux_public_entry_refuses_before_creating_state(self):
        with tempfile.TemporaryDirectory(prefix='wfc35-') as directory:
            root = Path(directory) / 'new'
            result = subprocess.run([sys.executable, str(ROOT / 'tools/windows_console_probe.py'),
                '--stage', 'idle', '--run-dir', str(root)], capture_output=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'native Windows is required', result.stderr)
            self.assertFalse(root.exists())


@unittest.skipUnless(sys.platform.startswith('linux'), 'portable fixture seam checks use Linux sockets')
class LinuxFixtures(unittest.TestCase):
    def test_actual_source_seams_without_console_claim(self):
        for stage, expected in (('idle', 'relay_finished'), ('rpc-fixture', 'rpc_result'),
                                ('queue-fixture', 'queue_result')):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix='wfc35-') as directory:
                root = Path(directory)
                if stage == 'rpc-fixture':
                    try:
                        with socket.socket(socket.AF_UNIX) as check:
                            check.bind(str(root / 'capability.sock'))
                    except OSError:
                        self.skipTest('sandbox denies AF_UNIX fixture server; accepted RPC NOT validated')
                env = probe.isolated_env(root)
                (root / 'herdr.toml').write_text('[terminal]\ndefault_shell="/bin/sh"\n')
                result = subprocess.run([sys.executable, str(ROOT / 'tools/windows_console_probe.py'),
                    '--internal', 'subject', '--stage', stage, '--run-dir', str(root)], env=env,
                    capture_output=True, timeout=9)
                events = probe.read_events(root)
                self.assertEqual(result.returncode, 0, events)
                records = [e for e in events if e['event'] == expected]
                self.assertEqual(len(records), 1, events)
                if stage == 'idle':
                    self.assertEqual(records[0]['waiter_count'], 0)
                    relay = next(e for e in events if e['event'] == 'detached_relay')
                    current = probe.wfc_platform.process_probe(relay['pid'])
                    self.assertTrue(current['state'] == 'exited' or current['identity'] != relay['creation'])
                elif stage == 'rpc-fixture':
                    self.assertEqual((records[0]['result'], records[0]['alive'], records[0]['owned']),
                                     ('accepted', True, True))
                else:
                    self.assertEqual((records[0]['result'], records[0]['launches']), ('delivered', 1))
                    self.assertEqual(sum(e['event'] == 'queue_fixture_entered' for e in events), 1)
                forbidden = ('commandline', 'title', 'environment', 'SECRET', 'params', 'fixture-thread')
                serialized = json.dumps(events)
                self.assertFalse(any(term in serialized for term in forbidden))

    def test_real_rpc_helper_unavailable_endpoint_fails_safely(self):
        with tempfile.TemporaryDirectory(prefix='wfc35-') as directory:
            root = Path(directory)
            env = probe.isolated_env(root)
            payload = dict(endpoint=env['HERDR_SOCKET_PATH'], method='session.snapshot', params={})
            command = "import herdr_backend,json,sys; print(json.dumps(herdr_backend.rpc(sys.argv[1],json.loads(sys.argv[2]))))"
            result = subprocess.run([sys.executable, '-c', command, str(ROOT / 'router.py'), json.dumps(payload)],
                cwd=ROOT, env=env, capture_output=True, timeout=4)
            self.assertEqual(result.returncode, 0)
            outcome = json.loads(result.stdout)
            self.assertIn((outcome['result'], outcome['category']),
                          (('definitely-not-sent', 'herdr_unavailable'), ('refused', 'herdr_ownership')))


@unittest.skipUnless(os.name == 'nt', 'native Windows interactive runner required; not Linux evidence')
class NativeProbe(unittest.TestCase):
    def invoke(self, root, *extra):
        return subprocess.Popen([sys.executable, str(ROOT / 'tools/windows_console_probe.py'),
            '--stage', 'observer-selftest', '--run-dir', str(root), *extra],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def assert_clean(self, root):
        events = probe.read_events(root)
        self.assertTrue(any(e['event'] == 'supervisor_end' and e['cleanup'] == 'owned_job_empty' for e in events), events)
        for event in events:
            process = event.get('process') if event['event'] == 'process_observed' else None
            if process:
                current = probe.wfc_platform.process_probe(process['pid'])
                self.assertTrue(current['state'] == 'exited' or
                                (current['state'] == 'alive' and current['identity'] != process['creation']), current)
        return events

    def test_visible_positive_control_and_job_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='wfc35-') as directory:
            root = Path(directory) / 'positive'
            child = self.invoke(root)
            self.assertEqual(child.wait(timeout=10), 0, probe.read_events(root))
            self.assertTrue(probe.positive_control_seen(self.assert_clean(root)))

    def test_stop_and_deadline_kill_owned_job(self):
        for trigger in ('STOP', 'deadline'):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory(prefix='wfc35-') as directory:
                root = Path(directory) / 'bounded'
                child = self.invoke(root, '--seconds', '1' if trigger == 'deadline' else '6')
                try:
                    if trigger == 'STOP':
                        self.assertTrue(probe.wait_file(root / 'observer-ready', root / 'STOP', time.monotonic() + 2))
                        (root / 'STOP').touch()
                    self.assertEqual(child.wait(timeout=9), 2)
                    events = self.assert_clean(root)
                    self.assertTrue(any(e['event'] == 'supervisor_end' and e['reason'] == trigger for e in events), events)
                finally:
                    if child.poll() is None:
                        child.kill()  # supervisor only; kill-on-close must contain its job
                        child.wait(timeout=1)

class ReviewRegressions(unittest.TestCase):
    """Deterministic versions of independent review S1/S2/S3; no native API calls."""
    class Child:
        def __init__(self, code):
            self.returncode = code
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            return self.returncode

    def fake_native(self, foreign=False):
        from contextlib import contextmanager
        from types import SimpleNamespace
        class Fake:
            job_name = 'fixture-job'
            def __init__(self):
                self.terminated = False
                self.k = SimpleNamespace(TerminateJobObject=self.terminate, CloseHandle=lambda h: True)
            def check(self, value):
                assert value
                return value
            def terminate(self, *args):
                self.terminated = True
                return True
            def job(self, observe_only=False):
                return 789 if observe_only else 123
            def duplicate_job(self, job):
                return 456
            def members(self, job):
                return [321] if foreign and not self.terminated else []
            def process(self, pid):
                return dict(pid=321, creation='windows:999', image='unrelated.exe')
            def parents(self):
                return {}
            @contextmanager
            def owned_process(self, job, pid, expected=None, diagnostic=None):
                if diagnostic is not None:
                    diagnostic.update(category='membership_refused', api='IsProcessInJob', winerror=None)
                yield None  # the numeric candidate is an outside-job replacement
        return Fake()

    def run_supervisor(self, root, native, observer_code=None):
        from types import SimpleNamespace
        from unittest.mock import patch
        class OS:
            name = 'nt'
            def __getattr__(self, key):
                return getattr(os, key)
        def spawn(root, role, *args):
            (root / 'observer-ready').touch()
            return self.Child(observer_code if role == 'observer' else 0)
        with patch.object(probe, 'os', OS()), patch.object(probe, 'Native', lambda: native), \
                patch.object(probe, 'isolated_env', lambda root: {}), patch.object(probe, 'spawn', spawn):
            return probe.supervise(SimpleNamespace(run_dir=root, stage='idle', seconds=6))

    def test_outside_job_replacement_is_not_published(self):
        with tempfile.TemporaryDirectory(prefix='wfc35-r2-') as directory:
            root = Path(directory) / 'run'
            self.run_supervisor(root, self.fake_native(foreign=True))
            events = probe.read_events(root)
            self.assertFalse(any(e['event'] == 'process_observed' for e in events), events)
            self.assertTrue(any(e['event'] == 'membership_identity_gap' for e in events), events)
            self.assertNotIn('unrelated.exe', json.dumps(events))
            self.assertNotIn('windows:999', (root / 'members.json').read_text())

    def test_observer_does_not_attach_to_outside_job_replacement(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(prefix='wfc35-r2-') as directory:
            root = Path(directory)
            native, calls = self.fake_native(), []
            native.k.FreeConsole = lambda: True
            native.windows = lambda owned: []
            native.console = lambda process, *args: calls.append(process) or dict(event='console_unavailable')
            native.observation_job = lambda root: 123
            (root / 'members.json').write_text(json.dumps([native.process(321)]))
            with patch.object(probe, 'Native', lambda: native), \
                    patch.object(probe.time, 'sleep', lambda _: (root / 'STOP').touch()):
                probe.observe(root, 'idle')
            self.assertEqual(calls, [])
            events = probe.read_events(root)
            self.assertTrue(any(e['event'] == 'membership_identity_gap' for e in events), events)
            self.assertNotIn('unrelated.exe', json.dumps(events))

    def test_observer_failure_dominates_same_iteration_phase_success(self):
        with tempfile.TemporaryDirectory(prefix='wfc35-r2-') as directory:
            root = Path(directory) / 'run'
            code = self.run_supervisor(root, self.fake_native(), observer_code=2)
            self.assertEqual(code, 2)
            end = next(e for e in probe.read_events(root) if e['event'] == 'supervisor_end')
            self.assertEqual(end['reason'], 'observer_failed')
            self.assertEqual(end['observer_returncode'], 2)
            self.assertEqual(end['cleanup'], 'owned_job_empty')

    def test_cancelled_inflight_reason_cannot_terminate_after_close(self):
        from unittest.mock import patch
        entered, release = threading.Event(), threading.Event()
        operations = []
        def delayed_reason(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test synchronization failed')
            return 'deadline'
        with patch.object(probe, 'stop_reason', delayed_reason):
            finished, watcher, outcome = probe.start_watchdog(Path('/unused'), 0,
                lambda: operations.append('terminate'))
            try:
                self.assertTrue(entered.wait(1))
                finished.set()
                watcher.join(timeout=.03)
                self.assertTrue(watcher.is_alive())
                operations.append('close')
            finally:
                release.set()
                watcher.join(timeout=1)
        self.assertFalse(watcher.is_alive())
        self.assertEqual(operations, ['close'])
        self.assertEqual(outcome, [])

    def test_native_gate_rejects_reuse_before_image_query_or_attachment(self):
        import ctypes as C
        from types import SimpleNamespace
        for case in ('outside', 'unknown', 'exited', 'wrong_creation', 'owned'):
            with self.subTest(case=case):
                calls, closed = [], []
                native = object.__new__(probe.Native)
                native.W = SimpleNamespace(BOOL=C.c_int)
                def member(handle, job, result):
                    self.assertEqual((handle, job), (555, 123))
                    C.cast(result, C.POINTER(C.c_int))[0] = case != 'outside'
                    return case != 'unknown'
                native.k = SimpleNamespace(OpenProcess=lambda *a: 555, IsProcessInJob=member,
                    WaitForSingleObject=lambda *a: 0 if case == 'exited' else 258,
                    CloseHandle=lambda h: closed.append(h),
                    AttachConsole=lambda pid: calls.append('attach') or False)
                native.identity = lambda h: 'windows:999' if case == 'wrong_creation' else 'windows:1'
                process = dict(pid=321, creation='windows:1', image='owned.exe')
                def metadata(handle, pid, creation=None):
                    self.assertFalse(closed)
                    calls.append('image')
                    return process
                native.metadata = metadata
                with native.owned_process(123, 321, process) as pin:
                    self.assertFalse(closed)
                    if pin:
                        native.console(pin[1], pin[0])
                self.assertEqual(calls, ['image', 'attach'] if case == 'owned' else [])
                self.assertEqual(closed, [555])

    def test_unresolved_callback_retains_its_own_handle_and_cannot_report_success(self):
        from unittest.mock import patch
        native, operations, closed = self.fake_native(), [], set()
        entered, release = threading.Event(), threading.Event()
        original_start, watched = probe.start_watchdog, []
        def terminate(handle, code):
            self.assertNotIn(handle, closed)
            operations.append(('terminate_start', handle))
            if handle == 456:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('test synchronization failed')
                self.assertNotIn(handle, closed)
            native.terminated = True
            operations.append(('terminate_end', handle))
            return True
        def close(handle):
            self.assertNotIn(handle, closed)
            operations.append(('close', handle))
            closed.add(handle)
            return True
        native.k.TerminateJobObject, native.k.CloseHandle = terminate, close
        def start(stop, deadline, callback, *args):
            result = original_start(stop, 0, callback, *args)
            watched.append(result)
            self.assertTrue(entered.wait(1))
            return result
        try:
            with tempfile.TemporaryDirectory(prefix='wfc35-r2-') as directory, \
                    patch.object(probe, 'start_watchdog', start):
                root = Path(directory) / 'run'
                before = time.monotonic()
                code = self.run_supervisor(root, native)
                self.assertLess(time.monotonic() - before, 2)
                self.assertEqual(code, 2)
                end = next(e for e in probe.read_events(root) if e['event'] == 'supervisor_end')
                self.assertEqual(end['reason'], 'watchdog_shutdown_unresolved')
                self.assertEqual(end['watchdog_shutdown'], 'unresolved')
                self.assertIn(123, closed)
                self.assertNotIn(456, closed)
        finally:
            release.set()
            for finished, thread, outcome in watched:
                finished.set()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
        self.assertIn(456, closed)
        self.assertLess(operations.index(('terminate_start', 456)), operations.index(('close', 123)))
        self.assertLess(operations.index(('terminate_end', 456)), operations.index(('close', 456)))

    def test_watchdog_failure_overrides_completed_phase(self):
        from unittest.mock import patch
        native = self.fake_native()
        original_start = probe.start_watchdog
        def start(stop, deadline, callback, *args):
            def fail():
                raise RuntimeError('controlled callback failure')
            result = original_start(stop, 0, fail, *args)
            result[1].join(timeout=1)
            self.assertFalse(result[1].is_alive())
            return result
        with tempfile.TemporaryDirectory(prefix='wfc35-r2-') as directory, \
                patch.object(probe, 'start_watchdog', start):
            root = Path(directory) / 'run'
            self.assertEqual(self.run_supervisor(root, native), 2)
            end = next(e for e in probe.read_events(root) if e['event'] == 'supervisor_end')
            self.assertEqual(end['reason'], 'watchdog_failed')
            self.assertEqual(end['watchdog_shutdown'], 'joined')
            self.assertEqual(end['cleanup'], 'owned_job_empty')

class NativeGapRegressions(unittest.TestCase):
    """Real native adapter/observer control flow with deterministic API outcomes."""
    def native(self):
        import ctypes as C
        from types import SimpleNamespace
        native = object.__new__(probe.Native)
        native.W = SimpleNamespace(BOOL=C.c_int)
        native.mode, native.calls = 'alive', []
        process = dict(pid=321, creation='windows:1', image='owned.exe')
        def member(handle, job, out):
            native.calls.append('membership')
            if native.mode == 'exit-during-member':
                native.mode = 'exited'
            C.cast(out, C.POINTER(C.c_int))[0] = native.mode != 'refused' and native.mode != 'exited'
            return native.mode != 'member-query-failed'
        def open_process(*args):
            native.calls.append('open')
            return 0 if native.mode == 'open-failed' else 555
        def wait(*args):
            native.calls.append('wait')
            return 0 if native.mode == 'exited' else (0xffffffff if native.mode == 'wait-failed' else 258)
        native.k = SimpleNamespace(OpenProcess=open_process, IsProcessInJob=member, WaitForSingleObject=wait,
            CloseHandle=lambda h: native.calls.append(('close', h)) or True, FreeConsole=lambda: True,
            AttachConsole=lambda pid: native.calls.append('attach') or False)
        native.identity = lambda h: 'windows:1'
        def metadata(handle, pid, creation=None):
            native.calls.append('image')
            return process.copy()
        native.metadata = metadata
        native.error_code = lambda: 87 if native.mode == 'open-failed' else 5
        native.observation_job = lambda root: 123
        native.windows = lambda owned: []
        native.console = lambda p, h: native.calls.append('console') or dict(event='console_unavailable')
        return native, process

    def observe(self, mode, initial=None):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix='wfc35-r6-') as directory:
            root = Path(directory)
            native, process = self.native()
            if initial is not None:
                native.mode = initial
            member = root / 'members.json'
            member.write_text(json.dumps([process]))
            rounds = []
            def advance(_):
                rounds.append(True)
                native.mode = mode
                if mode == 'replacement':
                    member.write_text(json.dumps([{**process, 'creation': 'windows:999', 'image': 'unrelated.exe'}]))
            with patch.object(probe, 'Native', lambda: native), patch.object(probe.time, 'sleep', advance), \
                    patch.object(probe, 'stop_reason', lambda *a: 'deadline' if len(rounds) >= 2 else None):
                probe.observe(root, 'observer-selftest')
            return native, probe.read_events(root), (root / 'STOP').exists()

    def test_verified_cached_exit_is_observed_without_STOP_or_reopen(self):
        for mode in ('exited', 'exit-during-member'):
            with self.subTest(mode=mode):
                native, events, stopped = self.observe(mode)
                self.assertFalse(stopped, events)
                self.assertFalse(any(e['event'] == 'membership_identity_gap' for e in events), events)
                exited = [e for e in events if e['event'] == 'owned_process_exited']
                self.assertEqual(len(exited), 1, events)
                self.assertEqual((exited[0]['candidate_pid'], exited[0]['creation'], exited[0]['proof']),
                                 (321, 'windows:1', 'retained_handle_signaled'))
                self.assertEqual(native.calls.count('open'), 1)
                self.assertEqual(native.calls.count('image'), 1)
                self.assertEqual(native.calls.count('console'), 1)
                self.assertEqual(native.calls.count(('close', 555)), 1)

    def test_cached_membership_refusal_is_not_an_exit(self):
        native, events, stopped = self.observe('refused')
        self.assertTrue(stopped)
        gap = next(e for e in events if e['event'] == 'membership_identity_gap')
        self.assertEqual((gap['candidate_pid'], gap['category'], gap['api']),
                         (321, 'membership_refused', 'IsProcessInJob'))
        self.assertFalse(any(e['event'] == 'owned_process_exited' for e in events))
        self.assertEqual(native.calls.count('console'), 1)

    def test_cached_query_failure_is_unknown_and_stops(self):
        for mode, api in (('wait-failed', 'WaitForSingleObject'), ('member-query-failed', 'IsProcessInJob')):
            with self.subTest(mode=mode):
                native, events, stopped = self.observe(mode)
                self.assertTrue(stopped)
                gap = next(e for e in events if e['event'] == 'membership_identity_gap')
                self.assertEqual((gap['category'], gap['api'], gap['winerror']), ('query_failed', api, 5))
                self.assertFalse(any(e['event'] == 'owned_process_exited' for e in events))
                self.assertEqual(native.calls.count('console'), 1)

    def test_changed_publication_never_observes_replacement(self):
        native, events, stopped = self.observe('replacement')
        self.assertTrue(stopped)
        gap = next(e for e in events if e['event'] == 'membership_identity_gap')
        self.assertEqual(gap['category'], 'published_identity_changed')
        self.assertNotIn('unrelated.exe', json.dumps(events))
        self.assertNotIn('windows:999', json.dumps(events))
        self.assertEqual(native.calls.count('open'), 1)
        self.assertEqual(native.calls.count('console'), 1)

    def test_console_exit_proof_and_query_or_attach_failures_remain_distinct(self):
        for case in ('exited', 'wait-failed', 'alive'):
            with self.subTest(case=case):
                native, process = self.native()
                with native.owned_process(123, 321, process) as pin:
                    self.assertIsNotNone(pin)
                    native.mode = case
                    event = probe.Native.console(native, process, pin[0])
                if case == 'exited':
                    self.assertEqual(event['event'], 'owned_process_exited')
                    self.assertEqual(event['proof'], 'retained_handle_signaled')
                    self.assertNotIn('attach', native.calls)
                elif case == 'wait-failed':
                    self.assertEqual((event['event'], event['category'], event['api'], event['winerror']),
                        ('console_identity_ambiguous', 'query_failed', 'WaitForSingleObject', 5))
                    self.assertNotIn('attach', native.calls)
                else:
                    self.assertEqual((event['event'], event['category'], event['api'], event['winerror']),
                        ('console_unavailable', 'attach_failed', 'AttachConsole', 5))

    def test_unverified_open_failure_is_not_verified_exit(self):
        native, process = self.native()
        native.mode, diagnostic = 'open-failed', {}
        # Entry disappearance (including ERROR_INVALID_PARAMETER) is not exit proof.
        with native.owned_process(123, 321, process, diagnostic=diagnostic) as pin:
            self.assertIsNone(pin)
        self.assertEqual(diagnostic, dict(category='query_failed', api='OpenProcess', winerror=87))
        self.assertNotIn('image', native.calls)
        native, events, stopped = self.observe('open-failed', initial='open-failed')
        self.assertTrue(stopped)
        self.assertFalse(any(e['event'] == 'owned_process_exited' for e in events))
        gap = next(e for e in events if e['event'] == 'membership_identity_gap')
        self.assertEqual((gap['candidate_pid'], gap['api'], gap['winerror']), (321, 'OpenProcess', 87))
        self.assertNotIn('image', native.calls)
        self.assertNotIn('console', native.calls)

class EnumWindowLivenessRegressions(unittest.TestCase):
    """Real ownership/observer/window/console seam; only Win32 calls are simulated."""
    def observe(self, position=1, result=0xffffffff, window='owned'):
        import ctypes as C
        from types import SimpleNamespace
        from unittest.mock import patch
        native, process = NativeGapRegressions().native()
        native_type = probe.Native
        native.W.DWORD = C.c_uint32
        native.callback = lambda fn: fn
        sampling = dict(active=False, waits=0, injected=0, owners=0)
        original_wait = native.k.WaitForSingleObject
        def wait(handle, timeout):
            if sampling['active']:
                sampling['waits'] += 1
                if sampling['waits'] == position and result is not None:
                    sampling['injected'] += 1
                    native.calls.append(('enum_wait', result))
                    if result == 0:
                        native.mode = 'exited'
                    return result
            return original_wait(handle, timeout)
        def owner(hwnd, output):
            if sampling['active']:
                sampling['owners'] += 1
                if window == 'vanished' and sampling['owners'] == 2:
                    return 0
            C.cast(output, C.POINTER(C.c_uint32))[0] = (999 if sampling['active'] and window == 'foreign' else process['pid'])
            return 1
        def enum(callback, param):
            sampling['active'] = True
            try:
                if window != 'empty':
                    self.assertTrue(callback(456, param))
            finally:
                sampling['active'] = False
            return True
        def members(output, count):
            output[0] = process['pid']
            return 1
        native.k.WaitForSingleObject = wait
        native.k.AttachConsole = lambda pid: native.calls.append('attach') or True
        native.k.SetConsoleCtrlHandler = lambda *args: True
        native.k.GetConsoleWindow = lambda: 456
        native.k.GetConsoleProcessList = members
        native.u = SimpleNamespace(EnumWindows=enum, GetWindowThreadProcessId=owner,
            IsWindowVisible=lambda hwnd: window != 'hidden')
        native.windows = lambda pins: native_type.windows(native, pins)
        def console(proc, handle):
            native.calls.append(('console_STOP_present', (root / 'STOP').exists()))
            return native_type.console(native, proc, handle)
        native.console = console
        def associated_process(pid):
            self.assertEqual(pid, process['pid'])  # no metadata query for foreign enum owner 999
            native.calls.append('associated_owner')
            return process.copy()
        native.process = associated_process
        with tempfile.TemporaryDirectory(prefix='wfc35-r7-') as directory:
            root = Path(directory)
            (root / 'members.json').write_text(json.dumps([process]))
            rounds = []
            with patch.object(probe, 'Native', lambda: native), \
                    patch.object(probe.time, 'sleep', lambda _: rounds.append(True)), \
                    patch.object(probe, 'stop_reason', lambda *args: 'deadline' if rounds else None):
                probe.observe(root, 'idle')
            return native, sampling, probe.read_events(root), (root / 'STOP').exists()

    def assert_unknown_sticks(self, position):
        native, sampling, events, stopped = self.observe(position)
        self.assertEqual(sampling['injected'], 1)
        self.assertTrue(stopped, events)
        diagnostics = [e for e in events if e.get('api') == 'WaitForSingleObject']
        self.assertTrue(diagnostics, events)
        self.assertEqual((diagnostics[0]['category'], diagnostics[0]['winerror'], diagnostics[0]['candidate_pid']),
                         ('query_failed', 5, 321))
        self.assertEqual(diagnostics[0]['check'], 'before_visibility' if position == 1 else 'after_visibility')
        self.assertFalse(any(e['event'] == 'owned_process_exited' for e in events))
        self.assertTrue(any(e['event'] == 'console_association' for e in events), events)
        self.assertIn('attach', native.calls)
        self.assertIn(('console_STOP_present', True), native.calls)
        # Unknown classification must be persisted before later successful association.
        self.assertLess(events.index(diagnostics[0]), next(i for i,e in enumerate(events) if e['event']=='console_association'))
        self.assertEqual(native.calls.count('open'), 1)
        self.assertEqual(native.calls.count('image'), 1)

    def test_first_callback_wait_failure_survives_later_console_success(self):
        self.assert_unknown_sticks(1)

    def test_second_callback_wait_failure_survives_later_console_success(self):
        self.assert_unknown_sticks(2)

    def test_propagated_STOP_cannot_be_accepted_as_simultaneous_phase_success(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        reviews = ReviewRegressions()
        native = reviews.fake_native()
        class OS:
            name = 'nt'
            def __getattr__(self, key):
                return getattr(os, key)
        with tempfile.TemporaryDirectory(prefix='wfc35-r7-boundary-') as directory:
            root = Path(directory) / 'run'
            class Phase(reviews.Child):
                def poll(self):
                    (root / 'STOP').touch()
                    return 0
            def spawn(root, role, *args):
                return reviews.Child(None) if role == 'observer' else Phase(0)
            def watchdog(stop, deadline, terminate, release):
                # Model a callback not yet scheduled when phase completion is sampled.
                thread = SimpleNamespace(join=lambda timeout: release(), is_alive=lambda: False)
                return threading.Event(), thread, []
            with patch.object(probe, 'os', OS()), patch.object(probe, 'Native', lambda: native),                     patch.object(probe, 'isolated_env', lambda root: {}), patch.object(probe, 'spawn', spawn),                     patch.object(probe, 'start_watchdog', watchdog):
                code = probe.supervise(SimpleNamespace(run_dir=root, stage='idle', seconds=6))
            self.assertEqual(code, 2)
            end = next(e for e in probe.read_events(root) if e['event'] == 'supervisor_end')
            self.assertEqual(end['reason'], 'STOP')
            self.assertEqual(end['cleanup'], 'owned_job_empty')

    def test_signaled_retained_object_stays_exit_proof_at_either_position(self):
        for position in (1, 2):
            with self.subTest(position=position):
                native, sampling, events, stopped = self.observe(position, result=0)
                self.assertEqual(sampling['injected'], 1)
                self.assertFalse(stopped, events)
                exited = [e for e in events if e['event'] == 'owned_process_exited']
                self.assertEqual(len(exited), 1, events)
                self.assertEqual((exited[0]['candidate_pid'], exited[0]['proof']), (321, 'retained_handle_signaled'))
                self.assertNotIn('attach', native.calls)

    def test_ordinary_no_visible_window_results_do_not_force_failure(self):
        for window in ('empty', 'foreign', 'hidden', 'vanished'):
            with self.subTest(window=window):
                native, sampling, events, stopped = self.observe(result=None, window=window)
                self.assertFalse(stopped, events)
                self.assertFalse(any(e.get('category') == 'query_failed' for e in events), events)
                self.assertFalse(any(e['event'] == 'owned_process_exited' for e in events))
                if window == 'foreign':
                    self.assertEqual(sampling['waits'], 0)
                    for event in events:
                        self.assertNotEqual(event.get('candidate_pid'), 999)
                        for field in ('owner', 'process'):
                            self.assertNotEqual((event.get(field) or {}).get('pid'), 999)
                if window == 'hidden':
                    windows = [e for e in events if e['event'] == 'owned_window']
                    self.assertTrue(windows)
                    self.assertFalse(windows[0]['visible'])

class MetadataExitBoundaryRegressions(unittest.TestCase):
    """Actual adapter/supervisor seam; fake native results, no process/image probing."""
    def supervise(self, scenario):
        from types import SimpleNamespace
        from unittest.mock import patch
        native, process = NativeGapRegressions().native()
        driver = ReviewRegressions().fake_native()
        for name in ('job', 'duplicate_job', 'parents', 'check'):
            setattr(native, name, getattr(driver, name))
        native.job_name = driver.job_name
        native.k.TerminateJobObject = driver.terminate
        native.members = lambda job: [] if driver.terminated else [321]
        failed_query = []
        turn = dict(second=scenario.startswith('first-seen'))
        original_metadata = native.metadata
        def metadata(handle, pid, creation=None):
            if turn['second']:
                if scenario == 'changed-creation':
                    native.calls.append('FOREIGN_IMAGE_QUERY')
                    return dict(pid=pid, creation=creation, image='unrelated.exe')
                failed_query.append(True)
                native.calls.append('image_error')
                if scenario in ('known-exit', 'first-seen-exit'):
                    native.mode = 'exited'
                elif scenario == 'unknown-wait':
                    native.mode = 'wait-failed'
                raise OSError('PRIVATE_R8_METADATA_ERROR')
            return original_metadata(handle, pid, creation)
        native.metadata = metadata
        native.identity = lambda h: 'windows:999' if scenario == 'changed-creation' and turn['second'] else 'windows:1'
        reviews = ReviewRegressions()
        class OS:
            name = 'nt'
            def __getattr__(self, key):
                return getattr(os, key)
        class Phase(reviews.Child):
            def poll(self):
                if not turn['second']:
                    turn['second'] = True
                    return None
                return 0
        def spawn(root, role, *args):
            return reviews.Child(None) if role == 'observer' else Phase(0)
        with tempfile.TemporaryDirectory(prefix='wfc35-r8-') as directory:
            root = Path(directory) / 'run'
            with patch.object(probe, 'os', OS()), patch.object(probe, 'Native', lambda: native), \
                    patch.object(probe, 'isolated_env', lambda root: {}), patch.object(probe, 'spawn', spawn):
                code = probe.supervise(SimpleNamespace(run_dir=root, stage='idle', seconds=6))
            return code, native, probe.read_events(root), json.loads((root / 'members.json').read_text()), len(failed_query)

    def test_known_metadata_error_plus_exact_postquery_exit_is_not_a_gap(self):
        code, native, events, members, queries = self.supervise('known-exit')
        self.assertEqual(code, 0, events)
        self.assertEqual(queries, 1)
        self.assertEqual(members, [])
        self.assertFalse(any(e['event'] == 'membership_identity_gap' for e in events), events)
        uncertainty = next(e for e in events if e['event'] == 'metadata_query_uncertain')
        self.assertEqual((uncertainty['candidate_pid'], uncertainty['api'], uncertainty['winerror'], uncertainty['post_query_state']),
                         (321, 'QueryFullProcessImageNameW', 5, 'exited'))
        exited = next(e for e in events if e['event'] == 'owned_process_exited')
        self.assertEqual((exited['candidate_pid'], exited['creation'], exited['proof']),
                         (321, 'windows:1', 'same_handle_signaled_after_metadata_query'))
        self.assertLess(events.index(uncertainty), events.index(exited))
        self.assertNotIn('PRIVATE_R8_METADATA_ERROR', json.dumps(events))

    def test_known_alive_metadata_error_remains_uncertain_gap(self):
        code, native, events, members, queries = self.supervise('known-alive')
        self.assertEqual(code, 2)
        gap = next(e for e in events if e['event'] == 'membership_identity_gap')
        self.assertEqual((gap['api'], gap['winerror'], gap['post_query_state']),
                         ('QueryFullProcessImageNameW', 5, 'alive'))
        self.assertFalse(gap.get('verified_exit', False))
        self.assertFalse(any(e['event'] == 'owned_process_exited' for e in events))
        self.assertNotIn('PRIVATE_R8_METADATA_ERROR', json.dumps(events))

    def test_postquery_unknown_wait_remains_gap(self):
        code, native, events, members, queries = self.supervise('unknown-wait')
        self.assertEqual(code, 2)
        gap = next(e for e in events if e['event'] == 'membership_identity_gap')
        self.assertEqual((gap['api'], gap['winerror']), ('QueryFullProcessImageNameW', 5))
        self.assertEqual((gap['post_query_state'], gap['post_query_api'], gap['post_query_winerror']),
                         ('unknown', 'WaitForSingleObject', 5))
        self.assertFalse(any(e['event'] == 'owned_process_exited' for e in events))

    def test_changed_creation_is_refused_before_foreign_image_query(self):
        code, native, events, members, queries = self.supervise('changed-creation')
        self.assertEqual(code, 2, events)
        self.assertNotIn('FOREIGN_IMAGE_QUERY', native.calls)
        self.assertNotIn('unrelated.exe', json.dumps(events))
        self.assertNotIn('windows:999', json.dumps(events))
        self.assertTrue(any(e['event'] == 'membership_identity_gap' for e in events))

    def test_first_seen_metadata_error_even_if_exited_has_no_known_exit_proof(self):
        for scenario in ('first-seen-exit', 'first-seen-alive'):
            with self.subTest(scenario=scenario):
                code, native, events, members, queries = self.supervise(scenario)
                self.assertEqual(code, 2)
                self.assertEqual(members, [])
                gap = next(e for e in events if e['event'] == 'membership_identity_gap')
                self.assertEqual((gap['api'], gap['winerror']), ('QueryFullProcessImageNameW', 5))
                self.assertFalse(any(e['event'] == 'owned_process_exited' for e in events))
                self.assertFalse(any(e['event'] == 'process_observed' for e in events))

    def test_postquery_creation_uncertainty_cannot_authorize_known_exit(self):
        for case in ('changed', 'failed'):
            with self.subTest(case=case):
                native, process = NativeGapRegressions().native()
                def metadata(*args, **kwargs):
                    native.mode = 'exited'
                    raise OSError('PRIVATE_R8_METADATA_ERROR')
                def identity(handle):
                    if native.mode == 'alive':
                        return 'windows:1'
                    if case == 'failed':
                        raise OSError('PRIVATE_R8_IDENTITY_ERROR')
                    return 'windows:999'
                native.metadata, native.identity = metadata, identity
                diagnostic = {}
                with native.owned_process(123, 321, process, diagnostic=diagnostic) as pin:
                    self.assertIsNone(pin)
                self.assertFalse(diagnostic.get('verified_exit', False))
                self.assertEqual(diagnostic['api'], 'QueryFullProcessImageNameW')
                self.assertEqual(diagnostic['winerror'], 5)
                self.assertEqual(diagnostic['post_query_state'], 'unknown' if case == 'failed' else 'identity_changed')
                self.assertNotIn('windows:999', json.dumps(diagnostic))
                self.assertNotIn('PRIVATE_R8', json.dumps(diagnostic))

    def test_observer_records_error_and_exact_exit_without_attaching(self):
        from unittest.mock import patch
        native, process = NativeGapRegressions().native()
        def metadata(*args, **kwargs):
            native.calls.append('image_error')
            native.mode = 'exited'
            raise OSError('PRIVATE_R8_METADATA_ERROR')
        native.metadata = metadata
        with tempfile.TemporaryDirectory(prefix='wfc35-r8-observe-') as directory:
            root = Path(directory)
            (root / 'members.json').write_text(json.dumps([process]))
            rounds = []
            with patch.object(probe, 'Native', lambda: native),                     patch.object(probe.time, 'sleep', lambda _: rounds.append(True)),                     patch.object(probe, 'stop_reason', lambda *args: 'deadline' if rounds else None):
                probe.observe(root, 'idle')
            events = probe.read_events(root)
            self.assertFalse((root / 'STOP').exists(), events)
            uncertainty = next(e for e in events if e['event'] == 'metadata_query_uncertain')
            exited = next(e for e in events if e['event'] == 'owned_process_exited')
            self.assertLess(events.index(uncertainty), events.index(exited))
            self.assertEqual(exited['proof'], 'same_handle_signaled_after_metadata_query')
            self.assertNotIn('PRIVATE_R8', json.dumps(events))
            self.assertNotIn('console', native.calls)
            self.assertEqual(native.calls.count('open'), 1)
            self.assertEqual(native.calls.count(('close', 555)), 1)

class SupervisorTelemetryRegressions(unittest.TestCase):
    """Actual supervisor control flow with deterministic boundary faults, no native APIs."""
    def test_setup_permission_failure_preserves_entry_exit_and_errno(self):
        import contextlib
        import io
        from unittest.mock import patch
        class OS:
            name = 'nt'
            def __getattr__(self, key):
                return getattr(os, key)
        original_write = Path.write_text
        def write(path, *args, **kwargs):
            if path.name == 'herdr.toml':
                raise PermissionError(13, 'R9_PRIVATE_MESSAGE', 'R9_PRIVATE_PATH')
            return original_write(path, *args, **kwargs)
        with tempfile.TemporaryDirectory(prefix='wfc35-r9-') as directory:
            root = Path(directory) / 'run'
            output = io.StringIO()
            with patch.object(probe, 'os', OS()), patch.object(Path, 'write_text', write), \
                    patch.object(probe, 'Native') as native, \
                    patch.object(sys, 'argv', ['probe', '--stage', 'idle', '--run-dir', str(root)]), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(probe.main(), 2)
                native.assert_not_called()
            events = probe.read_events(root)
            failure = next(e for e in events if e['event'] == 'supervisor_error')
            self.assertEqual((failure.get('operation'), failure.get('supervisor_phase'), failure.get('errno')),
                             ('write_fixture_config', 'setup', 13))
            self.assertIsNone(failure.get('winerror'))
            self.assertEqual([e['event'] for e in events], ['supervisor_error', 'probe_error'])
            recorded = output.getvalue() + '\n'.join(p.read_text() for p in root.glob('events-*.jsonl'))
            self.assertFalse('R9_PRIVATE_' in recorded)

    def fault(self, error, point='publish'):
        import contextlib
        import io
        from unittest.mock import patch
        reviews = ReviewRegressions()
        native = reviews.fake_native()
        if point == 'query':
            def members(job):
                if native.terminated:
                    return []
                raise error
            native.members = members
        if point == 'cleanup':
            def terminate(*args):
                native.terminated = True
                raise error
            native.k.TerminateJobObject = terminate
        with tempfile.TemporaryDirectory(prefix='wfc35-r9-') as directory:
            root = Path(directory) / 'run'
            output = io.StringIO()
            with contextlib.ExitStack() as patches, contextlib.redirect_stdout(output):
                if point == 'publish':
                    patches.enter_context(patch.object(probe.os, 'replace', side_effect=error))
                code = reviews.run_supervisor(root, native)
            events = probe.read_events(root)
            recorded = output.getvalue() + '\n' + '\n'.join(
                p.read_bytes().decode('utf-8', errors='replace') for p in root.rglob('*') if p.is_file())
            return code, native, events, recorded

    def assert_exception_hooks_unused(self, error, calls):
        import contextlib
        import io
        from unittest.mock import patch
        for entry in (False, True):
            with self.subTest(entry=entry):
                calls.clear()
                if entry:
                    with tempfile.TemporaryDirectory(prefix='wfc35-r10-') as directory:
                        root = Path(directory)
                        output = io.StringIO()
                        with patch.object(probe, 'internal', side_effect=error), \
                                patch.object(sys, 'argv', ['probe', '--stage', 'idle', '--internal', 'phase', '--run-dir', str(root)]), \
                                contextlib.redirect_stdout(output):
                            code = probe.main()
                        events = probe.read_events(root)
                        recorded = output.getvalue() + '\n'.join(
                            p.read_bytes().decode('utf-8', errors='replace') for p in root.rglob('*') if p.is_file())
                    failure = next(e for e in events if e['event'] == 'probe_error')
                    self.assertEqual((failure['operation'], failure['supervisor_phase']), ('invoke_command', 'entry'))
                else:
                    code, native, events, recorded = self.fault(error)
                    self.assertTrue(native.terminated)
                    failure = next(e for e in events if e['event'] == 'supervisor_error')
                    self.assertEqual(failure['operation'], 'publish_members_snapshot')
                    end = next(e for e in events if e['event'] == 'supervisor_end')
                    self.assertEqual((end['reason'], end['cleanup'], end['watchdog_shutdown']),
                                     ('probe_error', 'owned_job_empty', 'joined'))
                self.assertEqual(code, 2)
                self.assertEqual(calls, [], 'exception introspection hook executed')
                self.assertEqual((failure['category'], failure['errno'], failure['winerror']), ('Exception', None, None))
                self.assertNotIn('R10_PRIVATE', recorded)

    def test_exception_properties_never_run_before_cleanup_or_entry_exit(self):
        calls = []
        class PropertyError(PermissionError):
            @property
            def errno(self):
                calls.append('errno')
                print('R10_PRIVATE_PROPERTY')
                return 13
            @property
            def winerror(self):
                calls.append('winerror')
                print('R10_PRIVATE_PROPERTY')
                return 5
        self.assert_exception_hooks_unused(PropertyError(13, 'R10_PRIVATE_MESSAGE'), calls)

    def test_exception_attribute_hook_never_runs_before_cleanup_or_entry_exit(self):
        calls = []
        class AttributeErrorSentinel(PermissionError):
            def __getattribute__(self, name):
                if name in ('errno', 'winerror'):
                    calls.append(name)
                    print('R10_PRIVATE_ATTRIBUTE')
                    raise AssertionError('blocking-hook sentinel; never actually wait')
                return super().__getattribute__(name)
        self.assert_exception_hooks_unused(AttributeErrorSentinel(13, 'R10_PRIVATE_MESSAGE'), calls)

    def test_exception_class_hook_never_runs_before_cleanup_or_entry_exit(self):
        calls = []
        class ClassError(Exception):
            def __getattribute__(self, name):
                if name == '__class__':
                    calls.append(name)
                    print('R10_PRIVATE_CLASS')
                return super().__getattribute__(name)
        self.assert_exception_hooks_unused(ClassError('R10_PRIVATE_MESSAGE'), calls)

    def test_publish_permission_failure_has_fixed_operation_and_numeric_codes(self):
        error = PermissionError(13, 'R9_PRIVATE_MESSAGE', 'R9_PRIVATE_PATH')
        error.winerror = 5
        code, native, events, recorded = self.fault(error)
        self.assertEqual(code, 2)
        self.assertTrue(native.terminated)
        failure = next(e for e in events if e['event'] == 'supervisor_error')
        self.assertEqual((failure.get('operation'), failure.get('supervisor_phase'), failure['stage']),
                         ('publish_members_snapshot', 'run', 'idle'))
        self.assertEqual((failure['category'], failure.get('errno'), failure.get('winerror')), ('PermissionError', 13, 5))
        end = next(e for e in events if e['event'] == 'supervisor_end')
        self.assertEqual((end['reason'], end['cleanup'], end['watchdog_shutdown']), ('probe_error', 'owned_job_empty', 'joined'))
        self.assertFalse('R9_PRIVATE_' in recorded, 'exception values were persisted')

    def test_exception_markers_and_noninteger_codes_never_enter_logs(self):
        markers = ('R9_PRIVATE_TYPE', 'R9_PRIVATE_MESSAGE', 'R9_PRIVATE_ARGV', 'R9_PRIVATE_ENV', 'R9_PRIVATE_TITLE', 'R9_PRIVATE_PATH')
        formatted = []
        def forbidden(self):
            formatted.append(True)
            raise RuntimeError('must not format exception')
        secret_type = type(markers[0], (PermissionError,), {'__str__':forbidden, '__repr__':forbidden})
        class IntSubclass(int):
            pass
        for errno_value, winerror_value in ((13, None), (markers[1], markers[2]), (True, IntSubclass(5))):
            with self.subTest(numeric=type(errno_value) is int):
                error = secret_type(13, markers[1], markers[5])
                error.errno, error.winerror = errno_value, winerror_value
                error.args = (markers[2], markers[3], markers[4])
                code, native, events, recorded = self.fault(error)
                self.assertEqual(code, 2)
                self.assertTrue(native.terminated)
                failure = next(e for e in events if e['event'] == 'supervisor_error')
                self.assertEqual(failure['category'], 'Exception')
                self.assertIsNone(failure.get('errno'))
                self.assertIsNone(failure.get('winerror'))
                self.assertFalse(any(marker in recorded for marker in markers), 'unapproved exception data was persisted')
        self.assertEqual(formatted, [])

    def test_genuine_builtin_codes_require_exact_integers(self):
        class IntSubclass(int):
            pass
        for errno_value, winerror_value, expected in (
                (13, None, (13, None)), (13, 5, (13, 5)),
                ('R10_PRIVATE_ERRNO', 'R10_PRIVATE_WINERROR', (None, None)),
                (True, IntSubclass(5), (None, None))):
            with self.subTest(expected=expected):
                error = PermissionError(13, 'R10_PRIVATE_MESSAGE', 'R10_PRIVATE_PATH')
                error.errno, error.winerror = errno_value, winerror_value
                code, native, events, recorded = self.fault(error)
                self.assertEqual(code, 2)
                self.assertTrue(native.terminated)
                failure = next(e for e in events if e['event'] == 'supervisor_error')
                self.assertEqual(failure['category'], 'PermissionError')
                self.assertEqual((failure['errno'], failure['winerror']), expected)
                self.assertNotIn('R10_PRIVATE', recorded)

    def test_native_query_failure_label_and_errno_without_winerror(self):
        code, native, events, recorded = self.fault(PermissionError(13, 'R9_PRIVATE_MESSAGE'), point='query')
        self.assertEqual(code, 2)
        failure = next(e for e in events if e['event'] == 'supervisor_error')
        self.assertEqual((failure.get('operation'), failure.get('errno'), failure.get('winerror')),
                         ('enumerate_job_members', 13, None))
        self.assertTrue(native.terminated)
        self.assertFalse('R9_PRIVATE_' in recorded)

    def test_cleanup_failure_is_labeled_without_changing_outcome(self):
        code, native, events, recorded = self.fault(PermissionError(13, 'R9_PRIVATE_MESSAGE'), point='cleanup')
        self.assertEqual(code, 2)
        failure = next(e for e in events if e['event'] == 'cleanup_error')
        self.assertEqual((failure.get('operation'), failure.get('supervisor_phase'), failure.get('errno')),
                         ('terminate_owned_job', 'cleanup', 13))
        end = next(e for e in events if e['event'] == 'supervisor_end')
        self.assertEqual((end['reason'], end['cleanup']), ('cleanup_failed', 'owned_job_unconfirmed'))
        self.assertFalse('R9_PRIVATE_' in recorded)


if __name__ == '__main__':
    unittest.main()
