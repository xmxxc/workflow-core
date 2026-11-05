"""Tests for router.py through its command-line interface."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROUTER = Path(__file__).resolve().parent.parent / "router.py"


class Env:
    """A state dir, a worker cwd, and fake host programs on PATH that log their argv."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state = root / "state"
        self.cwd = root / "worktree"
        self.cwd.mkdir()
        self.task = root / "prompts" / "113-impl-r1.md"
        self.task.parent.mkdir()
        self.task.write_text("# task\n")
        self.report = root / "reports" / "113-impl-r1.md"
        self.report.parent.mkdir()
        self.calls = root / "calls.jsonl"
        self.bin = root / "bin"
        self.bin.mkdir()
        # Each fake logs its argv; FAKE_FAIL=<subcommand> makes that subcommand exit 1.
        for name in ("tmux", "codex"):
            fake = self.bin / name
            fake.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                            f"open({str(self.calls)!r}, 'a').write(json.dumps([{name!r}, *sys.argv[1:]]) + '\\n')\n"
                            f"sys.exit(1 if os.environ.get('FAKE_FAIL') == sys.argv[1] else 0)\n")
            fake.chmod(0o755)

    def run(self, *args, dispatch=None, timeout=10, env=None, cwd=None):
        full_env = {**os.environ, "WFC_STATE": str(self.state),
                    "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", **(env or {})}
        full_env.pop("WFC_DISPATCH", None)
        if dispatch:
            full_env["WFC_DISPATCH"] = dispatch
        return subprocess.run([sys.executable, str(ROUTER), *args], capture_output=True,
                              text=True, timeout=timeout, env=full_env, cwd=cwd)

    def json(self, *args, **kw):
        result = self.run(*args, **kw)
        if result.returncode not in (0, 3):
            raise AssertionError(f"router {args[0]} exited {result.returncode}: {result.stderr}")
        return json.loads(result.stdout)

    def dispatch(self, host="codex", **kw):
        return self.json("dispatch", "--host", host, "--task", str(self.task), "--cwd", str(self.cwd),
                         "--report", str(self.report), **kw)["dispatch"]

    def host_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def close(self):
        self.tmp.cleanup()


class MessageRoundTrip(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_worker_done_reaches_the_controller_once(self):
        dispatch = self.env.dispatch()
        self.env.report.write_text("# report\n")

        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        first = self.env.json("wait", "--timeout", "2")
        second = self.env.json("wait", "--timeout", "0.5")

        [message] = first["messages"]
        self.assertEqual((message["type"], message["dispatch"]), ("done", dispatch))
        self.assertEqual(message["report"], str(self.env.report))
        self.assertEqual(second, {"messages": [], "timedOut": True})

    def test_question_reaches_the_controller_and_the_reply_is_typed_into_the_worker(self):
        dispatch = self.env.dispatch()

        self.env.run("ask", "Red or blue?", "--options", "red,blue", dispatch=dispatch)
        [question] = self.env.json("wait", "--timeout", "2")["messages"]
        self.env.run("reply", str(question["id"]), "blue")

        self.assertEqual((question["type"], question["question"], question["options"]),
                         ("question", "Red or blue?", ["red", "blue"]))
        sends = [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"]]
        self.assertEqual(len(sends), 2)
        text_call, enter_call = sends
        self.assertEqual(text_call[2:4], ["-t", f"wfc-{dispatch}"])
        self.assertIn("blue", text_call[-1])
        self.assertIn(str(question["id"]), text_call[-1])
        self.assertEqual(enter_call, ["tmux", "send-keys", "-t", f"wfc-{dispatch}", "Enter"])


class Delivery(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_dispatch_starts_the_worker_in_its_own_tmux_session_with_its_identity(self):
        dispatch = self.env.dispatch("codex")

        [call] = self.env.host_calls()
        self.assertEqual(call[:4], ["tmux", "new-session", "-d", "-s"])
        self.assertEqual(call[4], f"wfc-{dispatch}")
        self.assertIn(f"WFC_DISPATCH={dispatch}", call)
        self.assertIn(f"WFC_STATE={self.env.state}", call)
        self.assertEqual(call[call.index("-c") + 1], str(self.env.cwd))
        codex = call[call.index("codex"):]
        self.assertEqual(codex[codex.index("--add-dir") + 1], str(self.env.state))
        # Unattended: sandboxed writes to the worktree and router state, never stop for approval.
        self.assertEqual(codex[codex.index("-s") + 1], "workspace-write")
        self.assertEqual(codex[codex.index("-a") + 1], "never")
        router = f"{shlex.quote(sys.executable)} {shlex.quote(str(ROUTER))}"
        brief = codex[-1]
        self.assertIn(shlex.quote(str(self.env.task)), brief)
        self.assertIn(f"{router} done --report {shlex.quote(str(self.env.report))}", brief)
        self.assertIn(f"{router} ask ", brief)
        self.assertIn("Never ask questions in plain text", brief)

    def test_report_directory_is_writable_and_paths_with_spaces_are_quoted(self):
        self.env.report = self.env.report.parent.parent / "my reports" / "113 impl.md"
        self.env.dispatch()

        [call] = self.env.host_calls()
        codex = call[call.index("codex"):]
        writable = [codex[i + 1] for i, arg in enumerate(codex) if arg == "--add-dir"]
        self.assertIn(str(self.env.report.parent), writable)
        self.assertIn(f"done --report {shlex.quote(str(self.env.report))}", codex[-1])

    def test_relative_state_dir_reaches_the_worker_as_an_absolute_path(self):
        root = self.env.state.parent
        self.env.dispatch(env={"WFC_STATE": "state"}, cwd=root)

        [call] = self.env.host_calls()
        self.assertIn(f"WFC_STATE={root / 'state'}", call)

    def test_dispatch_requires_a_report_path(self):
        result = self.env.run("dispatch", "--host", "codex", "--task", str(self.env.task),
                              "--cwd", str(self.env.cwd))

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.env.host_calls(), [])

    def test_failed_worker_launch_is_reported_without_a_traceback(self):
        result = self.env.run("dispatch", "--host", "codex", "--task", str(self.env.task),
                              "--cwd", str(self.env.cwd), "--report", str(self.env.report),
                              env={"FAKE_FAIL": "new-session"})

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)


class Replies(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.dispatch = self.env.dispatch()
        self.env.run("ask", "Red or blue?", dispatch=self.dispatch)
        [self.question] = self.env.json("wait", "--timeout", "2")["messages"]

    def sends(self):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"]]

    def test_a_question_is_answered_only_once(self):
        first = self.env.run("reply", str(self.question["id"]), "blue")
        second = self.env.run("reply", str(self.question["id"]), "red")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(len(self.sends()), 2)  # text + Enter of the first reply only

    def test_undeliverable_reply_fails_cleanly_and_can_be_retried(self):
        failed = self.env.run("reply", str(self.question["id"]), "blue", env={"FAKE_FAIL": "send-keys"})
        retried = self.env.run("reply", str(self.question["id"]), "blue")

        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn("Traceback", failed.stderr)
        self.assertEqual(retried.returncode, 0, retried.stderr)

    def test_multi_line_answer_is_typed_as_one_line(self):
        self.env.run("reply", str(self.question["id"]), "blue\nand keep the tests")

        text = self.sends()[0][-1]
        self.assertNotIn("\n", text)
        self.assertIn("blue", text)
        self.assertIn("and keep the tests", text)


class WorkerIdentity(unittest.TestCase):
    def test_messages_from_an_unknown_dispatch_are_rejected(self):
        env = Env()
        self.addCleanup(env.close)
        env.report.write_text("# report\n")

        done = env.run("done", "--report", str(env.report), dispatch="d-unknown")
        ask = env.run("ask", "Red or blue?", dispatch="d-unknown")

        self.assertNotEqual(done.returncode, 0)
        self.assertNotEqual(ask.returncode, 0)
        self.assertEqual(env.json("wait", "--timeout", "0.2")["messages"], [])


class Waiting(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_wait_blocks_until_a_late_message_arrives(self):
        dispatch = self.env.dispatch()
        self.env.report.write_text("# report\n")
        env = {**os.environ, "WFC_STATE": str(self.env.state)}
        waiter = subprocess.Popen([sys.executable, str(ROUTER), "wait", "--timeout", "10"],
                                  stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(waiter.kill)

        try:
            waiter.wait(timeout=1.5)
            self.fail("wait returned before any message existed")
        except subprocess.TimeoutExpired:
            pass
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        out, _ = waiter.communicate(timeout=5)

        self.assertEqual(waiter.returncode, 0)
        self.assertEqual(json.loads(out)["messages"][0]["type"], "done")

    def test_wait_timeout_is_reported_with_a_nonzero_exit_code(self):
        result = self.env.run("wait", "--timeout", "0.2")

        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout), {"messages": [], "timedOut": True})


if __name__ == "__main__":
    unittest.main()
