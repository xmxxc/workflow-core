"""Tests for the single-file `wfc` build: it runs the router's commands and briefs workers with its own path."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_router import Env  # noqa: E402  (fake hosts and tmux on PATH)


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.wfc = Path(cls.tmp.name).resolve() / "wfc"
        subprocess.run([sys.executable, str(ROOT / "build.py"), "--out", str(cls.wfc)], check=True,
                       capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_wfc(self, *args, env=None):
        return subprocess.run([str(self.wfc), *args], capture_output=True, text=True, env=env, timeout=30)

    def test_the_build_is_one_executable_file_that_reports_its_version(self):
        result = self.run_wfc("--version")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r"^wfc \d+\.\d+\.\d+")
        self.assertTrue(os.access(self.wfc, os.X_OK))

    def test_workers_are_briefed_to_report_through_the_same_file(self):
        env = Env()
        self.addCleanup(env.close)
        full = {**os.environ, "WFC_STATE": str(env.state), "WFC_CLAUDE_CONFIG": str(env.bin.parent / "none.json"),
                "PATH": f"{env.bin}{os.pathsep}{os.environ['PATH']}"}

        dispatched = json.loads(self.run_wfc("dispatch", "--host", "codex", "--task", str(env.task), "--cwd",
                                             str(env.cwd), "--report", str(env.report), env=full).stdout)
        [launch] = [c for c in env.host_calls() if c[:2] == ["tmux", "new-session"]]
        env.report.write_text("# r\n")
        done = self.run_wfc("done", "--report", str(env.report), env={**full, "WFC_DISPATCH": dispatched["dispatch"]})
        waited = json.loads(self.run_wfc("wait", "--timeout", "2", env=full).stdout)

        self.assertIn(f"{self.wfc} done --report", launch[-1])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(waited["messages"][0]["type"], "done")


if __name__ == "__main__":
    unittest.main()
