"""Core CLI and native platform contracts; no executable bits or Unix shebangs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class CorePlatform(unittest.TestCase):
    def test_core_import_does_not_require_unix_modules(self):
        result = subprocess.run([sys.executable, '-c',
                                 "import sys; sys.modules['fcntl'] = None; import router; import wfc; wfc.main(['--help'])"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_only_cli_creates_no_state_in_unicode_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / '状态 with spaces'
            env = {**os.environ, 'WFC_STATE': str(state), 'PYTHONUTF8': '1'}
            for command in (['--help'], ['--version'], ['status']):
                result = subprocess.run([sys.executable, str(ROOT / 'wfc.py'), *command],
                                        env=env, capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(state.exists())
            self.assertEqual(json.loads(result.stdout)['dispatches'], [])

    def test_lock_wait_is_bounded_and_process_crash_releases_it(self):
        import wfc_platform
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '锁 with spaces'
            ready = Path(tmp) / 'ready'
            code = ("import os,sys,time;from pathlib import Path;import wfc_platform;"
                    "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600);wfc_platform.acquire_lock(fd);"
                    "Path(sys.argv[2]).write_text('ready');time.sleep(30)")
            child = subprocess.Popen([sys.executable, '-c', code, str(path), str(ready)], cwd=ROOT)
            try:
                import time
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    start = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        wfc_platform.acquire_lock(fd, timeout=.15)
                    self.assertLess(time.monotonic() - start, 2)
                    child.kill()
                    child.wait(timeout=5)
                    wfc_platform.acquire_lock(fd, timeout=1)
                finally:
                    os.close(fd)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    def test_parallel_lock_holders_serialize_shared_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / 'counter'
            counter.write_text('0', encoding='utf-8')
            code = '''import os,sys
from pathlib import Path
import wfc_platform
p=Path(sys.argv[1])
for _ in range(15):
    fd=os.open(str(p)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        wfc_platform.acquire_lock(fd)
        n=int(p.read_text(encoding='utf-8'))
        p.write_text(str(n+1),encoding='utf-8')
    finally:
        os.close(fd)
'''
            children = [subprocess.Popen([sys.executable, '-c', code, str(counter)], cwd=ROOT) for _ in range(4)]
            try:
                for child in children:
                    self.assertEqual(child.wait(timeout=15), 0)
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
            self.assertEqual(counter.read_text(encoding='utf-8'), '60')

    def test_worker_callback_preserves_literal_unicode_argv(self):
        import wfc_platform
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / '参数 with spaces.json'
            values = ["space 中文", "a'b", '; touch injected', '$env:PATH & | >', 'a" "b', '', 'tail\\']
            code = ("import json,sys;from pathlib import Path;"
                    "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:],ensure_ascii=False),encoding='utf-8')")
            command = wfc_platform.callback_command([sys.executable, '-c', code, str(output), *values])
            shell = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command'] if os.name == 'nt' else ['sh', '-c']
            result = subprocess.run([*shell, command], capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), values)

    def test_brief_callback_overrides_stale_host_shell_context(self):
        import router
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "工具 space'quoted"
            root.mkdir()
            output = root / 'captured.json'
            capture = root / 'capture.py'
            capture.write_text("import json,os,sys;from pathlib import Path;"
                               "Path(" + repr(str(output)) + ").write_text(json.dumps({"
                               "'dispatch':os.environ.get('WFC_DISPATCH'),"
                               "'state':os.environ.get('WFC_STATE'),'argv':sys.argv[1:]},"
                               "ensure_ascii=False),encoding='utf-8')", encoding='utf-8')
            state = root / 'correct state'
            with patch.object(router, 'PROGRAM', capture), patch.dict(os.environ, {'WFC_STATE': str(state)}):
                brief = router.worker_brief('d-current', root / 'task.md', root / 'report.md', attempt='a-current')
            if os.name == 'nt':
                done = brief.split('run in PowerShell: ')[1].split('; If you need a decision,')[0]
                ask = brief.split('run in PowerShell: ')[2].split('; then end your turn;')[0]
                ask = ask.replace('<question text>', 'May I finish?')
                shell = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command']
            else:
                done = brief.split('and run: ')[1].split('; If you need a decision,')[0]
                ask = brief.split('run: ')[2].split('; then end your turn;')[0]
                ask = ask.replace('<question>', 'May I finish?').replace(' [--options a,b]', '')
                shell = ['sh', '-c']
            for kind, command in (('done', done), ('ask', ask)):
                with self.subTest(callback=kind):
                    result = subprocess.run([*shell, command], env={**os.environ, 'WFC_DISPATCH': 'd-old',
                                            'WFC_STATE': str(root / 'old state')}, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    observed = json.loads(output.read_text(encoding='utf-8'))
                    self.assertEqual(observed['dispatch'], 'd-current')
                    self.assertEqual(observed['state'], str(state.resolve()))
                    self.assertEqual(observed['argv'][0], kind)
                    self.assertIn('a-current', observed['argv'])

    @unittest.skipUnless(os.name == 'nt', 'requires native Windows process handles')
    def test_windows_creation_identity_and_exit_use_read_only_queries(self):
        import wfc_platform
        import router
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
        try:
            probe = wfc_platform.windows_process(child.pid)
            self.assertEqual(probe['state'], 'alive')
            self.assertTrue(probe['identity'].startswith('windows:'))
            self.assertEqual(wfc_platform.windows_process(child.pid)['identity'], probe['identity'])
            self.assertTrue(router.waiter_alive({'pid': child.pid, 'process_start': probe['identity']}))
            self.assertFalse(router.waiter_alive({'pid': child.pid, 'process_start': 'windows:wrong-creation'}))
            child.kill()
            child.wait(timeout=5)
            self.assertEqual(wfc_platform.windows_process(child.pid)['state'], 'exited')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_detached_child_survives_starting_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'detached done'
            child_code = ("import time;from pathlib import Path;time.sleep(.4);"
                          "Path(" + repr(str(marker)) + ").write_text('done')")
            parent_code = ("import subprocess,sys;import wfc_platform;"
                           "subprocess.Popen([sys.executable,'-c',sys.argv[1]],stdin=subprocess.DEVNULL,"
                           "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**wfc_platform.detached_options())")
            subprocess.run([sys.executable, '-c', parent_code, child_code], cwd=ROOT, check=True, timeout=10)
            import time
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(marker.read_text(), 'done')

    def test_native_windows_does_not_auto_update_host_trust(self):
        if os.name != 'nt':
            self.skipTest('Windows trust configuration boundary')
        import router
        self.assertIsNone(router.trust_claude_worktree(ROOT))

    @unittest.skipUnless(os.name == 'nt', 'requires Windows callback brief')
    def test_multiword_question_from_brief_reaches_core_as_one_argument(self):
        import router
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'report.md'
            brief = router.worker_brief('d-demo', Path(tmp) / 'task.md', report, attempt='a-demo')
            ask = brief.split('run in PowerShell: ')[2].split('; then end your turn;')[0]
            ask = ask.replace('<question text>', 'May I retry?')
            # Core reaches dispatch validation rather than argparse extra-argument rejection.
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ask],
                                    env={**os.environ, 'WFC_STATE': str(Path(tmp) / 'state')},
                                    capture_output=True, timeout=15)
            self.assertNotIn(b'unrecognized arguments', result.stderr)
            self.assertIn(b'dispatch', result.stderr.lower())
