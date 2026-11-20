"""Public offline checker controls; these do not observe a Windows desktop."""
import json
import os
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'tools/check_foreground_evidence.py'


def sample():
    # Synthetic FILETIME identities at 2026-11-18T21:00:00Z and +1 second.
    entry = {'pid': 101, 'creation': 'windows:134395092000000000'}
    child = {'pid': 102, 'creation': 'windows:134395092010000000'}
    return {
        'version': 1,
        'capture_start': '2026-11-18T21:00:00+00:00',
        'capture_end': '2026-11-18T21:00:10+00:00',
        'clock_uncertainty_ms': 0,
        'lost_events': 0,
        'entry': entry,
        'processes': [
            {**entry, 'parent': None, 'basename': 'python.exe',
             'stage': 'entry', 'creation_flags': 0, 'exited_at': None},
            {**child, 'parent': entry, 'basename': 'helper.exe',
             'stage': 'helper', 'creation_flags': 0, 'exited_at': None},
        ],
        'shows': [{'at': '2026-11-18T21:00:02+00:00', 'hwnd': '0x1234',
                   'class': 'CONTROLLED_TEST_WINDOW', 'owner': child}],
    }


class ForegroundEvidence(unittest.TestCase):
    def check(self, data, raw=None):
        with tempfile.TemporaryDirectory(prefix='wfc-window-evidence-') as tmp:
            path = Path(tmp) / 'evidence.json'
            path.write_text(json.dumps(data) if raw is None else raw, encoding='utf-8')
            result = subprocess.run([sys.executable, str(TOOL), str(path)],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.stderr, '')
        return result.returncode, json.loads(result.stdout)

    def test_reused_pid_never_supplies_missing_owner_creation(self):
        data = sample()
        data['shows'][0]['owner']['creation'] = 'windows:134395092020000000'
        code, result = self.check(data)
        self.assertEqual(code, 1)
        self.assertIn('owner_identity_missing', result['reasons'])
        self.assertEqual(result['native_attribution'], 'pending')

    def test_consistent_ledger_is_only_ready_for_review(self):
        code, result = self.check(sample())
        self.assertEqual(code, 0)
        self.assertEqual(result, {'verdict': 'review_ready', 'native_attribution': 'pending', 'reasons': []})

    def test_missing_timezone_is_invalid(self):
        data = sample()
        data['shows'][0]['at'] = '13:28:59.971524'
        self.assertEqual(self.check(data)[0], 2)

    def test_missing_parent_creation_is_pending(self):
        data = sample()
        data['processes'][1]['parent'] = {'pid': 101, 'creation': 'windows:134395091990000000'}
        code, result = self.check(data)
        self.assertEqual(code, 1)
        self.assertIn('parent_identity_missing', result['reasons'])

    def test_foreign_root_does_not_prove_wfc_causation(self):
        data = sample()
        data['processes'][1]['parent'] = None
        code, result = self.check(data)
        self.assertEqual(code, 1)
        self.assertIn('owner_chain_does_not_reach_entry', result['reasons'])

    def test_parent_cycle_is_pending(self):
        data = sample()
        data['processes'][1]['parent'] = dict(data['shows'][0]['owner'])
        self.assertIn('parent_cycle', self.check(data)[1]['reasons'])

    def test_parent_exited_before_child_is_pending(self):
        data = sample()
        data['processes'][0]['exited_at'] = '2026-11-18T21:00:00.5+00:00'
        self.assertIn('parent_lifetime_contradiction', self.check(data)[1]['reasons'])

    def test_owner_exited_before_show_is_pending(self):
        data = sample()
        data['processes'][1]['exited_at'] = '2026-11-18T21:00:01.5Z'
        self.assertIn('owner_lifetime_or_clock_ambiguous', self.check(data)[1]['reasons'])

    def test_filetime_submicrosecond_is_not_rounded_to_match(self):
        data = sample()
        key = 'windows:134395092010000001'
        data['processes'][1]['creation'] = key
        data['shows'][0]['owner']['creation'] = key
        data['shows'][0]['at'] = '2026-11-18T21:00:01.0000000Z'
        self.assertIn('owner_lifetime_or_clock_ambiguous', self.check(data)[1]['reasons'])
        data['shows'][0]['at'] = '2026-11-18T21:00:01.0000001Z'
        self.assertEqual(self.check(data)[0], 0)

    def test_lost_events_and_unknown_flags_stay_pending(self):
        data = sample()
        data['lost_events'] = 1
        data['processes'][1]['creation_flags'] = None
        code, result = self.check(data)
        self.assertEqual(code, 1)
        self.assertEqual(result['reasons'], ['capture_events_lost', 'creation_flags_missing'])

    def test_empty_or_out_of_scope_show_is_not_an_absence_pass(self):
        data = sample()
        data['shows'] = []
        self.assertIn('show_event_missing', self.check(data)[1]['reasons'])
        data = sample()
        data['shows'][0]['at'] = '2026-11-18T21:00:11Z'
        self.assertIn('show_outside_capture_or_clock_ambiguous', self.check(data)[1]['reasons'])

    def test_clock_uncertainty_prevents_false_lifetime_match(self):
        data = sample()
        data['clock_uncertainty_ms'] = 2000
        self.assertIn('owner_lifetime_or_clock_ambiguous', self.check(data)[1]['reasons'])

    def test_forbidden_fields_do_not_echo_values_or_key_names(self):
        for value in ({'environment': {'TOKEN': 'PRIVATE_SENTINEL'}},
                      {'argv': ['C:/PRIVATE_SENTINEL/host.exe']},
                      {'nonce': 'PRIVATE_SENTINEL'}):
            with self.subTest(field=next(iter(value))):
                data = sample()
                data.update(value)
                code, result = self.check(data)
                self.assertEqual(code, 2)
                self.assertEqual(result['reasons'], ['schema_or_input_invalid'])
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))

    def test_duplicate_keys_and_excess_size_are_invalid(self):
        self.assertEqual(self.check(None, raw='{"version":1,"version":1}')[0], 2)
        self.assertEqual(self.check(None, raw=' ' * (512 * 1024 + 1))[0], 2)

    @unittest.skipIf(os.name == 'nt', 'this checks Linux refusal, not the native probe')
    def test_native_probe_refuses_linux_before_reading_kernel(self):
        result = subprocess.run([sys.executable, str(ROOT / 'tools/probe_windows_foreground.py'),
                                 '--kernel', '/does-not-exist', '--kernel-sha256', '0' * 64],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, '')
        self.assertEqual(json.loads(result.stdout), {'verdict': 'pending', 'reason': 'native_windows_required'})


class ModeledProbeReporting(unittest.TestCase):
    """Actual CLI/report control flow with modeled native/process boundaries only."""
    def run_model(self, cleanup_failure=None, launch_failure=None, observation_status='usable', observation_cleanup_failure=False,
                  attach_failure=False, root_cleanup_failure=False, audit=None):
        spec = importlib.util.spec_from_file_location('foreground_probe_model', ROOT / 'tools/probe_windows_foreground.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        import bundled_runtime
        import wfc_platform
        real_temporary_directory = tempfile.TemporaryDirectory
        identity = {'pid': 123, 'creation': 'windows:134395092000000000'}
        scopes = []

        class Scope:
            def __init__(self, *args, **kwargs):
                self.inner = real_temporary_directory(*args, **kwargs)
                self.name = self.inner.name
                scopes.append(self)
                self.index = len(scopes)

            def __enter__(self):
                return self.name

            def cleanup(self):
                self.inner.cleanup()
                if cleanup_failure == self.index:
                    raise PermissionError('synthetic temporary cleanup failure')

            def __exit__(self, *args):
                self.cleanup()

        class Job:
            handle = 1234
            kernel = SimpleNamespace(CloseHandle=lambda handle: True)

            def attach(self, pid):
                if attach_failure:
                    raise PermissionError('synthetic Job attachment failure')

            def stop(self, record):
                record('job_empty', active=0)

        launches = []
        api = SimpleNamespace(CreateProcess=lambda *args: (1, 2, 123, 456))

        def popen(argv, **kwargs):
            index = len(launches) + 1
            launches.append(index)
            if audit is not None:
                audit['popen_calls'] = len(launches)
            if launch_failure == index:
                raise PermissionError('synthetic later launch failure')
            api.CreateProcess(None, None, None, None, True, kwargs['creationflags'], None, None, None)
            path = Path(argv[-1])
            data = json.loads(path.read_text())
            (path.parent / 'guardian.json').write_text(json.dumps([
                {'phase': 'python_createprocess_return', 'python_createprocess_flags': data['runtime_flags'], 'child': identity}]))
            (path.parent / 'runtime.json').write_text(json.dumps([
                {'phase': 'python_createprocess_return', 'python_createprocess_flags': 0, 'child': identity},
                {'phase': 'conpty_io_pass'}]))
            def failed_wait(**kwargs):
                raise subprocess.TimeoutExpired('controlled root', kwargs['timeout'])
            def failed_kill():
                raise PermissionError('synthetic root kill failure')
            return SimpleNamespace(pid=123, returncode=None if root_cleanup_failure else 0, stdin=io.BytesIO(),
                                   poll=lambda: None if root_cleanup_failure else 0,
                                   wait=failed_wait if root_cleanup_failure else lambda **kwargs: 0,
                                   kill=failed_kill if root_cleanup_failure else lambda: None)

        report = {'status': observation_status, 'native_attribution': 'pending'}
        if observation_cleanup_failure:
            report.update(status='incomplete', errors=[{'phase': 'positive_destroy', 'error_type': 'PermissionError'}],
                          positive_controls=[{'hwnd': '0x101', 'destroyed': False}])
        observer = SimpleNamespace(pump=lambda: None, close=lambda: None, positive=lambda phase: None,
                                   summary=lambda: report)
        try:
            with real_temporary_directory(prefix='wfc-probe-report-model-') as tmp:
                binary = Path(tmp) / 'controlled-kernel'
                binary.write_bytes(b'synthetic; never executed')
                output = io.StringIO()
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.dict(sys.modules, {'_winapi': api}))
                    for target, name, value in [
                        (probe, 'os', SimpleNamespace(name='nt')),
                        (probe, 'self_identity', lambda: identity),
                        (probe, 'clock_anchor', lambda: {'utc_before': '2026-11-18T21:00:00Z', 'uptime_ms': 1, 'utc_after': '2026-11-18T21:00:00Z'}),
                        (probe, 'WindowObserver', lambda *args, **kwargs: observer),
                        (probe.tempfile, 'TemporaryDirectory', Scope),
                        (probe.subprocess, 'Popen', popen),
                        (bundled_runtime, 'WindowsContainment', Job),
                        (wfc_platform, 'detached_options', lambda: {'creationflags': 0x208, 'close_fds': True}),
                        (wfc_platform, 'process_identity', lambda pid: identity['creation']),
                    ]:
                        stack.enter_context(patch.object(target, name, value))
                    stack.enter_context(patch.object(sys, 'argv', [str(probe.__file__), '--kernel', str(binary),
                                        '--kernel-sha256', hashlib.sha256(binary.read_bytes()).hexdigest()]))
                    stack.enter_context(contextlib.redirect_stdout(output))
                    code = probe.main()
                return code, json.loads(output.getvalue())
        finally:
            for scope in scopes:
                scope.inner.cleanup()

    def test_second_temporary_cleanup_failure_retains_both_trials(self):
        code, result = self.run_model(cleanup_failure=2)
        self.assertEqual(code, 1)
        self.assertEqual(len(result['trials']), 2)
        self.assertEqual(result['trials'][0]['stages']['runtime'][-1]['phase'], 'conpty_io_pass')
        self.assertEqual(result['trials'][1]['stages']['runtime'][-1]['phase'], 'conpty_io_pass')
        self.assertTrue(any(error['phase'] == 'temporary_cleanup' for error in result['trials'][1]['errors']))

    def test_first_temporary_cleanup_failure_retains_first_and_later_trial(self):
        code, result = self.run_model(cleanup_failure=1)
        self.assertEqual(code, 1)
        self.assertEqual(len(result['trials']), 2)
        self.assertEqual(result['trials'][0]['stages']['runtime'][-1]['phase'], 'conpty_io_pass')
        self.assertEqual(result['trials'][1]['stages']['runtime'][-1]['phase'], 'conpty_io_pass')
        self.assertTrue(any(error['phase'] == 'temporary_cleanup' for error in result['trials'][0]['errors']))

    def test_later_launch_failure_retains_prior_flags_identity_and_cleanup(self):
        code, result = self.run_model(launch_failure=2)
        self.assertEqual(code, 1)
        self.assertEqual(len(result['trials']), 2)
        first, second = result['trials']
        self.assertEqual(first['entry_guardian_python_flags'], 0x208)
        self.assertEqual(first['guardian']['pid'], 123)
        self.assertTrue(first['root_reaped'])
        self.assertEqual(first['cleanup'], [{'phase': 'job_empty', 'active': 0}])
        self.assertEqual(second['entry']['pid'], 123)
        self.assertIn({'phase': 'trial', 'error_type': 'PermissionError'}, second['errors'])

    def test_first_launch_failure_is_retained_and_later_trial_still_recorded(self):
        code, result = self.run_model(launch_failure=1)
        self.assertEqual(code, 1)
        self.assertEqual(len(result['trials']), 2)
        self.assertTrue(result['trials'][0]['errors'])
        self.assertFalse(result['trials'][0]['root_created'])
        self.assertEqual(result['trials'][1]['stages']['runtime'][-1]['phase'], 'conpty_io_pass')

    def test_normal_model_retains_two_records_without_claiming_native_attribution(self):
        code, result = self.run_model()
        self.assertEqual(code, 0)
        self.assertEqual(len(result['trials']), 2)
        self.assertEqual(result['io_flags_result'], 'pass')
        self.assertEqual(result['native_attribution'], 'pending')
        self.assertTrue(all(item['root_created'] and item['root_reaped'] for item in result['trials']))

    def test_failed_attach_and_unreaped_root_block_later_launch_without_identity(self):
        audit = {}
        code, result = self.run_model(attach_failure=True, root_cleanup_failure=True, audit=audit)
        self.assertEqual(code, 1)
        self.assertEqual(audit['popen_calls'], 1)
        first, second = result['trials']
        self.assertTrue(first['root_created'])
        self.assertFalse(first['root_reaped'])
        self.assertNotIn('guardian', first)
        self.assertEqual(first['cleanup'], [{'phase': 'job_empty', 'active': 0}])
        self.assertEqual([error['phase'] for error in first['errors']],
                         ['trial', 'root_reap', 'root_kill', 'root_emergency_reap'])
        self.assertEqual(second['errors'], [{'phase': 'previous_cleanup_unconfirmed', 'error_type': 'SkippedTrial'}])
        self.assertFalse(second['root_created'])
        self.assertEqual(first['stages']['runtime'][-1]['phase'], 'conpty_io_pass')

    def test_failed_attach_with_confirmed_reap_allows_later_launch(self):
        audit = {}
        code, result = self.run_model(attach_failure=True, audit=audit)
        self.assertEqual(code, 1)
        self.assertEqual(audit['popen_calls'], 2)
        self.assertTrue(all(item['root_created'] and item['root_reaped'] for item in result['trials']))
        self.assertTrue(all('guardian' not in item for item in result['trials']))

    def test_incomplete_windows_never_pass_on_successful_modeled_io(self):
        code, result = self.run_model(observation_status='incomplete')
        self.assertEqual(code, 1)
        self.assertEqual(result['io_flags_result'], 'pass')
        self.assertEqual(result['window_observation_result'], 'incomplete')

    def test_uncleaned_positive_window_prevents_later_tree_launch(self):
        code, result = self.run_model(observation_cleanup_failure=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(result['trials']), 2)
        self.assertEqual(result['trials'][1]['errors'],
                         [{'phase': 'previous_cleanup_unconfirmed', 'error_type': 'SkippedTrial'}])
        self.assertNotIn('guardian', result['trials'][1])


class ModeledWindowObservation(unittest.TestCase):
    """Real observer policy against a modeled native window/event API, no UI."""
    @contextlib.contextmanager
    def observe(self, emit_controls=True, destroy_failure=False):
        spec = importlib.util.spec_from_file_location('foreground_window_model', ROOT / 'tools/probe_windows_foreground.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        owner = {'pid': 123, 'creation': 'windows:134395092000000000'}
        snapshots = {}
        instances = []

        class API:
            def __init__(self, job):
                self.pending, self.destroyed, self.closed = [], [], False
                self.serial = 0
                instances.append(self)

            def start(self, callback):
                self.callback = callback

            def snapshot(self, hwnd):
                data = snapshots[hwnd]
                if isinstance(data, Exception):
                    raise data
                return json.loads(json.dumps(data))

            def create_control(self):
                self.serial += 1
                hwnd = 0x100 + self.serial
                snapshots[hwnd] = record(hwnd, 'STATIC', owner, False)
                return {'hwnd': hex(hwnd), 'owner': owner, 'thread': 456}

            def show_control(self, control):
                if emit_controls:
                    self.pending.append((int(control['hwnd'], 16), 0, 0, 10000))

            def destroy_control(self, control):
                if destroy_failure:
                    raise PermissionError('PRIVATE_SENTINEL')
                self.destroyed.append(control['hwnd'])

            def pump(self):
                while self.pending:
                    self.callback(*self.pending.pop(0))

            def close(self):
                self.closed = True

        def record(hwnd, classname, identity, member):
            return {'hwnd': hex(hwnd), 'class': classname, 'owner': identity,
                    'owner_sampled_at': '2026-11-18T21:00:10Z',
                    'identity_binding': 'sampled_after_event', 'in_job': member,
                    'owner_lifetime': {'creation_filetime': int(identity['creation'][8:]),
                                       'exit_filetime': None, 'sampled_state': 'alive'}}

        tick = [0]
        def monotonic():
            tick[0] += .1
            return tick[0]
        anchor = {'utc_before': '2026-11-18T21:00:10Z', 'utc_after': '2026-11-18T21:00:10Z',
                  'uptime_ms': 10000, 'tick_padding_ms': 32, 'system_time_increment_100ns': 156250}
        with patch.object(probe, 'NativeWindowAPI', API), \
             patch.object(probe, 'clock_anchor', lambda: anchor), \
             patch.object(probe, 'time', SimpleNamespace(monotonic=monotonic, sleep=lambda seconds: None)):
            observer = probe.WindowObserver(None, 1234)
            try:
                yield observer, instances[0], snapshots, record
            finally:
                observer.close()

    def test_owned_positive_and_new_owner_hwnd_are_observed_separately(self):
        with self.observe() as (observer, api, snapshots, record):
            observer.positive('before')
            new_owner = {'pid': 999, 'creation': 'windows:134395092010000000'}
            snapshots[0x777] = record(0x777, 'CASCADIA_HOSTING_WINDOW_CLASS', new_owner, False)
            snapshots[0x888] = record(0x888, 'CONTROLLED_CHILD_CLASS', new_owner, True)
            snapshots[0x999] = record(0x999, 'UNRELATED_PRIVATE_CLASS', new_owner, False)
            api.pending.extend((hwnd, 0, 0, 10000) for hwnd in (0x777, 0x888, 0x999))
            observer.pump()
            observer.positive('after')
        result = observer.summary()
        self.assertEqual(result['status'], 'usable')
        self.assertEqual(len(result['positive_controls']), 2)
        self.assertTrue(all(item['seen'] and item['destroyed'] for item in result['positive_controls']))
        self.assertEqual([item['relation'] for item in result['events']], ['unattributed_terminal_candidate', 'owned_job'])
        self.assertEqual(result['events'][0]['owner'], new_owner)
        self.assertIn('owner_lifetime', result['events'][0])
        self.assertNotIn('UNRELATED_PRIVATE_CLASS', json.dumps(result))
        self.assertEqual(result['native_attribution'], 'pending')

    def test_silent_hook_without_positive_event_is_incomplete(self):
        with self.observe(emit_controls=False) as (observer, api, snapshots, record):
            observer.positive('before')
            observer.positive('after')
        self.assertEqual(observer.summary()['status'], 'incomplete')
        self.assertEqual(len(api.destroyed), 2)
        self.assertIn('positive_before_missing', observer.summary()['reasons'])

    def test_owner_query_loss_is_not_usable_or_echoed(self):
        with self.observe() as (observer, api, snapshots, record):
            observer.positive('before')
            snapshots[0x777] = PermissionError('PRIVATE_SENTINEL')
            api.pending.append((0x777, 0, 0, 10000))
            observer.pump()
            observer.positive('after')
        result = observer.summary()
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['lost_events'], 1)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))

    def test_callback_budget_and_positive_cleanup_failure_are_incomplete(self):
        with self.observe(destroy_failure=True) as (observer, api, snapshots, record):
            observer.positive('before')
            snapshots[0x777] = record(0x777, 'UNRELATED_PRIVATE_CLASS',
                                      {'pid': 999, 'creation': 'windows:134395092000000000'}, False)
            for _ in range(513):
                observer.event(0x777, 0, 0, 10000)
            observer.pump()
        result = observer.summary()
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('capture_budget_exhausted', result['reasons'])
        self.assertTrue(any(item['phase'] == 'positive_destroy' for item in result['errors']))
        self.assertEqual(result['events'], [])

    def test_owner_creation_after_event_keeps_clock_uncertainty(self):
        with self.observe() as (observer, api, snapshots, record):
            observer.positive('before')
            identity = {'pid': 999, 'creation': 'windows:134395092200000000'}
            snapshots[0x777] = record(0x777, 'CASCADIA_HOSTING_WINDOW_CLASS', identity, False)
            api.pending.append((0x777, 0, 0, 10000))
            observer.pump()
            observer.positive('after')
        self.assertEqual(observer.summary()['status'], 'incomplete')
        self.assertIn('owner_lifetime_or_clock_uncertain', observer.summary()['reasons'])

    def test_same_coarse_tick_does_not_claim_one_millisecond_precision(self):
        with self.observe() as (observer, api, snapshots, record):
            observer.positive('before')
            snapshots[0x777] = record(0x777, 'CASCADIA_HOSTING_WINDOW_CLASS',
                                      {'pid': 999, 'creation': 'windows:134395092000000000'}, False)
            api.pending.append((0x777, 0, 0, 10000))
            observer.pump()
            observer.positive('after')
        # Known synthetic event16ms before the UTC anchor shares its coarse tick.
        self.assertLessEqual(observer.summary()['events'][0]['event_utc_earliest'],
                             '2026-11-18T21:00:09.984000+00:00')

    def test_clock_failure_after_create_retains_uncleaned_owned_handle(self):
        with self.observe(destroy_failure=True) as (observer, api, snapshots, record):
            with patch.dict(observer.positive.__globals__,
                            {'clock_anchor': lambda: (_ for _ in ()).throw(PermissionError('PRIVATE_SENTINEL'))}):
                observer.positive('before')
        result = observer.summary()
        self.assertEqual(len(result['positive_controls']), 1)
        self.assertEqual(result['positive_controls'][0]['hwnd'], '0x101')
        self.assertFalse(result['positive_controls'][0]['destroyed'])

    def test_unknown_tick_resolution_retains_raw_event_and_incomplete_mapping(self):
        with self.observe() as (observer, api, snapshots, record):
            observer.positive('before')
            snapshots[0x777] = record(0x777, 'CASCADIA_HOSTING_WINDOW_CLASS',
                                      {'pid': 999, 'creation': 'windows:134395092000000000'}, False)
            anchor = {'utc_before': '2026-11-18T21:00:10Z', 'utc_after': '2026-11-18T21:00:10Z', 'uptime_ms': 10000}
            with patch.dict(observer.timing.__globals__, {'clock_anchor': lambda: anchor}):
                observer.event(0x777, 0, 0, 10000)
            observer.positive('after')
        result = observer.summary()
        self.assertEqual(result['status'], 'incomplete')
        self.assertIn('tick_resolution_unknown', result['reasons'])
        self.assertEqual(result['events'][0]['owner']['pid'], 999)
        self.assertIsNone(result['events'][0]['event_utc_earliest'])


if __name__ == '__main__':
    unittest.main()
