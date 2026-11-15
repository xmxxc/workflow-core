"""Exercise the built payload and native launchers, independent of Unix chmod."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parent.parent


class WindowsDistribution(unittest.TestCase):
    def test_same_source_payload_and_native_shell_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / '工具 with spaces'
            result = subprocess.run([sys.executable, str(ROOT / 'build.py'), '--windows-dir', str(directory)],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            app = directory / 'wfc.pyz'
            with zipfile.ZipFile(app) as archive:
                for module in ('wfc.py', 'router.py', 'host_launch.py', 'wfc_platform.py', 'herdr_backend.py'):
                    self.assertEqual(archive.read(module), (ROOT / module).read_bytes())
            env = {**os.environ, 'WFC_STATE': str(Path(tmp) / 'state'), 'PYTHONUTF8': '1'}
            native = subprocess.run([sys.executable, str(app), '--version'], env=env, capture_output=True)
            self.assertEqual(native.returncode, 0, native.stderr)
            self.assertIn(b'0.4.6', native.stdout)
            if os.name == 'nt':
                # Pin python on PATH to the interpreter used to build this test artifact.
                env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + env['PATH']
                cmd = subprocess.run([os.environ['COMSPEC'], '/d', '/s', '/c',
                                      '""' + str(directory / 'wfc.cmd') + '" status --scope "范围 with spaces""'],
                                     env=env, capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(cmd.returncode, 0, cmd.stderr)
                self.assertEqual(json.loads(cmd.stdout)['dispatches'], [])
                ps_path = str(directory / 'wfc.cmd').replace("'", "''")
                powershell = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                            "& '" + ps_path + "' status --scope '范围 with spaces'; exit $LASTEXITCODE"],
                                           env=env, capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(powershell.returncode, 0, powershell.stderr)
                self.assertEqual(json.loads(powershell.stdout)['dispatches'], [])
            # Rebuilding into an existing install is refused rather than partially overwriting it.
            repeated = subprocess.run([sys.executable, str(ROOT / 'build.py'), '--windows-dir', str(directory)],
                                      capture_output=True)
            self.assertNotEqual(repeated.returncode, 0)
