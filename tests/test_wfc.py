"""Tests for the single-file `wfc` build: it runs every command, briefs workers with its own path,
and installs the skills it carries."""

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
        cls.wfc = Path(cls.tmp.name) / "wfc"
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

    def test_controller_checks_are_subcommands_too(self):
        result = self.run_wfc("label", "--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--from", result.stdout)

    def test_install_skills_installs_the_workflow_skill_and_only_missing_prototypes(self):
        home = Path(self.tmp.name) / "home"
        claude, codex = home / ".claude" / "skills", home / ".codex" / "skills"
        claude.mkdir(parents=True)
        codex.mkdir(parents=True)
        (claude / "ai-coding").mkdir()
        (claude / "ai-coding" / "SKILL.md").write_text("the user's own ai-coding\n")

        result = self.run_wfc("install-skills", env={**os.environ, "HOME": str(home)})

        self.assertEqual(result.returncode, 0, result.stderr)
        for skills in (claude, codex):
            self.assertTrue((skills / "surper-coding" / "SKILL.md").is_file())
            self.assertTrue((skills / "surper-coding" / "references" / "router.md").is_file())
            self.assertTrue((skills / "live-verify" / "SKILL.md").is_file())
        self.assertEqual((claude / "ai-coding" / "SKILL.md").read_text(), "the user's own ai-coding\n")
        self.assertTrue((codex / "ai-coding" / "SKILL.md").is_file())

    def test_install_skills_leaves_a_linked_development_install_alone(self):
        home = Path(self.tmp.name) / "home-dev"
        skills = home / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "surper-coding").symlink_to(ROOT / "skills" / "surper-coding")

        result = self.run_wfc("install-skills", env={**os.environ, "HOME": str(home)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((skills / "surper-coding").is_symlink())
        self.assertIn("surper-coding", result.stdout)


if __name__ == "__main__":
    unittest.main()
