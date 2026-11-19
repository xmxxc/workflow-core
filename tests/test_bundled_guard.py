"""Guardian launch contracts; mocked calls do not prove native window behavior."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

import bundled_runtime as runtime
import router


NO_WINDOW = 0x08000000


class GuardianLaunch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='wfc guardian model ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.spec_path = self.root / 'launch.spec'
        self.host_spec = self.root / 'host.spec'
        self.snapshot = self.root / 'runtime.json'
        self.cgroup_root = self.root / 'cgroups'
        self.cgroup_root.mkdir()
        self.spec = {'unit': 'd-controlled', 'generation': 'g-controlled', 'attempt': 'a-controlled',
                     'nonce': 'n-controlled', 'token': 'controlled-private-test-capability',
                     'snapshot': str(self.snapshot), 'host_spec': str(self.host_spec),
                     'cgroup_root': str(self.cgroup_root)}
        self.write(self.spec_path, self.spec)
        self.write(self.host_spec, {})
        self.write(self.snapshot, {key: self.spec[key] for key in ('unit', 'generation', 'attempt', 'nonce')})
        self.child = Mock(pid=4242)
        self.child.wait.return_value = 0
        self.spawn = Mock(return_value=self.child)
        self.containment = Mock()

    def write(self, path, value):
        Path(path).write_text(json.dumps(value), encoding='utf-8')

    def run_guard(self, windows):
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime.wfc_platform, 'WINDOWS', windows))
            stack.enter_context(patch.object(runtime.wfc_platform, 'process_identity', return_value='controlled-creation'))
            stack.enter_context(patch.object(runtime.subprocess, 'CREATE_NO_WINDOW', NO_WINDOW, create=True))
            stack.enter_context(patch.object(runtime.subprocess, 'Popen', self.spawn))
            stack.enter_context(patch.object(runtime, 'publish', side_effect=self.write))
            stack.enter_context(patch.object(runtime, 'cgroup_root', return_value=self.cgroup_root))
            stack.enter_context(patch.object(runtime, 'LinuxContainment', return_value=self.containment))
            runtime.guard(str(self.spec_path))

    def test_windows_runtime_gets_only_create_no_window_with_existing_stdio(self):
        self.run_guard(True)
        self.assertEqual(self.spawn.call_count, 1)
        argv = self.spawn.call_args.args[0]
        self.assertEqual(argv, [sys.executable, str(router.PROGRAM), '_bundled-runtime', str(self.spec_path)])
        self.assertEqual(self.spawn.call_args.kwargs, {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
                         'stderr': subprocess.DEVNULL, 'close_fds': True, 'creationflags': NO_WINDOW})
        self.child.wait.assert_called_once_with()
        self.assertFalse(self.spec_path.exists())
        self.assertFalse(self.host_spec.exists())
        self.assertEqual(runtime.read_json(self.snapshot)['cleanup'], 'unknown')

    def test_posix_runtime_options_and_owned_containment_cleanup_are_preserved(self):
        self.run_guard(False)
        self.assertEqual(self.spawn.call_args.kwargs, {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
                                                     'stderr': subprocess.DEVNULL, 'close_fds': True})
        self.containment.stop.assert_called_once()
        self.containment.dispose.assert_called_once_with()
        self.assertEqual(list(self.cgroup_root.iterdir()), [])
        self.assertFalse(self.spec_path.exists())
        self.assertFalse(self.host_spec.exists())

    def test_windows_launch_failure_preserves_unknown_cleanup_and_removes_transient_specs(self):
        self.spawn.side_effect = OSError('controlled launch failure')
        with self.assertRaisesRegex(OSError, 'controlled launch failure'):
            self.run_guard(True)
        self.assertEqual(self.spawn.call_args.kwargs['creationflags'], NO_WINDOW)
        self.assertFalse(self.spec_path.exists())
        self.assertFalse(self.host_spec.exists())
        receipt = runtime.read_json(self.snapshot)
        self.assertEqual((receipt['state'], receipt['cleanup']), ('faulted', 'unknown'))
        self.assertNotIn('receiptSignature', receipt)
        self.containment.stop.assert_not_called()

    def test_posix_launch_failure_still_runs_owned_containment_cleanup(self):
        self.spawn.side_effect = OSError('controlled launch failure')
        with self.assertRaisesRegex(OSError, 'controlled launch failure'):
            self.run_guard(False)
        self.assertNotIn('creationflags', self.spawn.call_args.kwargs)
        self.containment.stop.assert_called_once()
        self.containment.dispose.assert_called_once_with()
        self.assertEqual(list(self.cgroup_root.iterdir()), [])
        self.assertFalse(self.spec_path.exists())
        self.assertFalse(self.host_spec.exists())

    def test_windows_guard_preserves_matching_authenticated_runtime_cleanup(self):
        def exited():
            spec = runtime.read_json(self.spec_path)
            value = {key: spec[key] for key in ('unit', 'generation', 'attempt', 'nonce', 'guardian')}
            runtime.publish_cleanup(spec, {**value, 'state': 'closed', 'cleanup': 'confirmed'})
            return 0
        self.child.wait.side_effect = exited
        self.run_guard(True)
        receipt = runtime.read_json(self.snapshot)
        self.assertEqual((receipt['state'], receipt['cleanup']), ('closed', 'confirmed'))
        self.assertTrue(runtime.confirmed_cleanup({**self.spec, 'capabilityHash': runtime.capability_digest(self.spec['token'])}, receipt))
        self.assertFalse(self.spec_path.exists())
        self.assertFalse(self.host_spec.exists())
