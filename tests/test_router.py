"""Tests for router.py through its command-line interface."""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
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
        # The fake tmux keeps one marker file per session: new-session creates it (after
        # FAKE_LAUNCH_DELAY seconds) and has-session checks it. capture-pane prints FAKE_SCREEN's
        # file if set, else a fresh screen each time (a busy worker).
        self.screen = root / "screen.txt"
        self.sessions = root / "sessions"
        self.sessions.mkdir()
        for name in ("tmux", "codex", "opencode", "claude"):
            fake = self.bin / name
            fake.write_text(
                f"#!{sys.executable}\nimport json, os, sys, time\n"
                f"open({str(self.calls)!r}, 'a').write(json.dumps([{name!r}, *sys.argv[1:]]) + '\\n')\n"
                f"if os.environ.get('FAKE_FAIL') == sys.argv[1]:\n"
                f"    sys.exit(1)\n"
                f"sessions = {str(self.sessions)!r}\n"
                f"if {name!r} == 'tmux' and sys.argv[1] == 'new-session':\n"
                f"    time.sleep(float(os.environ.get('FAKE_LAUNCH_DELAY', '0')))\n"
                f"    open(os.path.join(sessions, sys.argv[sys.argv.index('-s') + 1]), 'w').close()\n"
                f"if {name!r} == 'tmux' and sys.argv[1] == 'has-session':\n"
                f"    sys.exit(0 if os.path.exists(os.path.join(sessions, sys.argv[sys.argv.index('-t') + 1])) else 1)\n"
                f"if {name!r} == 'tmux' and sys.argv[1] == 'capture-pane':\n"
                f"    screen = os.environ.get('FAKE_SCREEN')\n"
                f"    print(open(screen).read() if screen else f'Working ({{time.time_ns()}})')\n")
            fake.chmod(0o755)

    def end_session(self, dispatch):
        (self.sessions / f"wfc-{dispatch}").unlink()

    def run(self, *args, dispatch=None, timeout=10, env=None, cwd=None):
        # Tests never read or write the real ~/.claude.json.
        full_env = {**os.environ, "WFC_STATE": str(self.state),
                    "WFC_CLAUDE_CONFIG": str(self.bin.parent / "no-claude-config.json"),
                    "CODEX_HOME": str(self.bin.parent / "codex-home"),
                    "WFC_OPENCODE_DB": str(self.bin.parent / "opencode.db"),
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

    def dispatch(self, host="codex", extra_args=(), **kw):
        return self.json("dispatch", "--host", host, "--task", str(self.task), "--cwd", str(self.cwd),
                         "--report", str(self.report), *extra_args, **kw)["dispatch"]

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
        self.assertIn("Never ask questions in plain text or with an interactive question tool", brief)

    def test_every_host_gets_its_worker_identity_and_a_created_report_directory(self):
        self.env.report = self.env.report.parent / "new-scope" / "113-impl-r1.md"
        for host in ("codex", "opencode", "claude"):
            with self.subTest(host=host):
                self.env.calls.unlink(missing_ok=True)
                dispatch = self.env.dispatch(host)

                [call] = self.env.host_calls()
                self.assertIn(f"WFC_DISPATCH={dispatch}", call)
                self.assertIn(f"WFC_STATE={self.env.state}", call)
                self.assertTrue(self.env.report.parent.is_dir())

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

    def test_opencode_worker_is_never_stopped_by_external_directory_prompts(self):
        self.env.dispatch("opencode")

        [call] = self.env.host_calls()
        config = json.loads(next(a for a in call if a.startswith("OPENCODE_CONFIG_CONTENT="))
                            .split("=", 1)[1])
        # User decision 2026-11-05: OpenCode's directory rules are tool prompts, not a sandbox
        # (shell writes bypass them), and git/pnpm touch ~ constantly, so unattended workers allow all.
        # "*" overrides any narrower rule in the user's own config, which OpenCode merges in.
        self.assertEqual(config["permission"]["external_directory"], {"*": "allow"})
        # Test environments only (user decision 2026-11-07): .env reads must not stop the worker either.
        self.assertEqual(config["permission"]["read"], {"*.env": "allow", "*.env.*": "allow"})
        opencode = call[call.index("opencode"):]
        self.assertEqual(opencode[1], str(self.env.cwd))
        self.assertIn("done --report", opencode[opencode.index("--prompt") + 1])

    def test_claude_worker_runs_in_auto_mode_with_state_and_report_dirs_added(self):
        self.env.dispatch("claude")

        [call] = self.env.host_calls()
        claude = call[call.index("claude"):]
        self.assertIn("done --report", claude[1])  # before --add-dir, which takes every following argument
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "auto")
        added = claude[claude.index("--add-dir") + 1:]
        self.assertEqual(added, [str(self.env.state), str(self.env.report.parent)])

    def test_model_is_passed_to_each_host_in_its_own_flag(self):
        for host, flag in (("codex", "-m"), ("opencode", "-m"), ("claude", "--model")):
            with self.subTest(host=host):
                self.env.calls.unlink(missing_ok=True)
                self.env.dispatch(host, extra_args=["--model", "some/model"])

                [call] = self.env.host_calls()
                command = call[call.index(host):]
                self.assertEqual(command[command.index(flag) + 1], "some/model")

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
        env = {**os.environ, "WFC_STATE": str(self.env.state),
               "PATH": f"{self.env.bin}{os.pathsep}{os.environ['PATH']}"}
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


class Watchdog(unittest.TestCase):
    """wait reports workers that exited or stopped changing without done or a pending question."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.dispatch = self.env.dispatch()
        self.env.screen.write_text("› Which color do you prefer?\n\n  Worked for 4s\n")
        self.still = {"FAKE_SCREEN": str(self.env.screen)}

    def wait(self, env=None, timeout="1.5"):
        return self.env.json("wait", "--timeout", timeout, "--stall-after", "0.3", env=env)

    def test_worker_whose_session_ended_without_done_is_reported_once(self):
        self.env.end_session(self.dispatch)
        first = self.wait()
        second = self.wait(timeout="0.6")

        [message] = first["messages"]
        self.assertEqual((message["type"], message["dispatch"]), ("exited", self.dispatch))
        self.assertTrue(second["timedOut"])

    def test_worker_whose_screen_stopped_changing_is_reported_once_with_its_screen(self):
        first = self.wait(env=self.still)
        second = self.wait(env=self.still, timeout="0.8")

        [message] = first["messages"]
        self.assertEqual((message["type"], message["dispatch"]), ("stalled", self.dispatch))
        self.assertIn("Which color do you prefer?", message["screen"])
        self.assertTrue(second["timedOut"])

    def test_busy_worker_is_not_reported(self):
        self.assertTrue(self.wait(timeout="1")["timedOut"])

    def test_worker_waiting_for_an_answer_is_not_reported(self):
        self.env.run("ask", "Red or blue?", dispatch=self.dispatch)
        [question] = self.wait()["messages"]

        self.assertEqual(question["type"], "question")
        self.assertTrue(self.wait(env=self.still, timeout="1")["timedOut"])

    def test_finished_worker_is_not_reported(self):
        self.env.report.write_text("# report\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)
        [done] = self.wait()["messages"]

        self.assertEqual(done["type"], "done")
        self.env.end_session(self.dispatch)
        self.assertTrue(self.wait(env=self.still, timeout="1")["timedOut"])

    def test_a_second_stall_on_the_same_screen_is_reported_after_the_worker_moved_on(self):
        [first] = self.wait(env=self.still)["messages"]
        self.assertTrue(self.wait(timeout="0.6")["timedOut"])  # busy: the screen changes every poll
        [second] = self.wait(env=self.still)["messages"]

        self.assertEqual((first["type"], second["type"]), ("stalled", "stalled"))

    def test_worker_still_starting_up_is_not_reported_as_exited(self):
        env = {**os.environ, "WFC_STATE": str(self.env.state), "FAKE_LAUNCH_DELAY": "2",
               "PATH": f"{self.env.bin}{os.pathsep}{os.environ['PATH']}"}
        starting = subprocess.Popen([sys.executable, str(ROUTER), "dispatch", "--host", "codex",
                                     "--task", str(self.env.task), "--cwd", str(self.env.cwd),
                                     "--report", str(self.env.report)], stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(starting.kill)
        time.sleep(0.5)  # the new dispatch exists, its tmux session does not yet

        result = self.wait(timeout="1")
        starting.communicate(timeout=10)

        self.assertTrue(result["timedOut"], result)


class ContinueDispatch(unittest.TestCase):
    """continue gives a finished worker its next task in the same session."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.dispatch = self.env.dispatch()
        self.env.report.write_text("# report r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)
        [done] = self.env.json("wait", "--timeout", "1")["messages"]
        self.assertEqual(done["type"], "done")
        self.next_task = self.env.task.with_name("113-impl-r2.md")
        self.next_task.write_text("# fix round\n")
        self.next_report = self.env.report.with_name("113-impl-r2.md")

    def continue_with(self, report=None, env=None):
        return self.env.run("continue", self.dispatch, "--task", str(self.next_task),
                            "--report", str(report or self.next_report), env=env)

    def sends(self):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"]]

    def test_next_task_is_typed_into_the_same_session(self):
        result = self.continue_with()

        self.assertEqual(result.returncode, 0, result.stderr)
        text_call, enter_call = self.sends()
        self.assertEqual(text_call[2:4], ["-t", f"wfc-{self.dispatch}"])
        self.assertIn(shlex.quote(str(self.next_task)), text_call[-1])
        self.assertIn(f"done --report {shlex.quote(str(self.next_report))}", text_call[-1])
        self.assertEqual(enter_call, ["tmux", "send-keys", "-t", f"wfc-{self.dispatch}", "Enter"])

    def test_a_continued_worker_is_watched_and_reports_again(self):
        self.continue_with()
        self.env.end_session(self.dispatch)

        [exited] = self.env.json("wait", "--timeout", "1.5", "--stall-after", "0.3")["messages"]

        self.assertEqual((exited["type"], exited["dispatch"]), ("exited", self.dispatch))

    def test_the_next_report_must_stay_in_the_dispatch_report_directory(self):
        elsewhere = self.env.report.parent.parent / "elsewhere" / "r2.md"

        result = self.continue_with(report=elsewhere)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(self.env.report.parent), result.stderr)
        self.assertEqual(self.sends(), [])

    def test_continuing_a_worker_whose_session_ended_fails_cleanly(self):
        self.env.end_session(self.dispatch)

        result = self.continue_with()

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.sends(), [])


class ContinueBeforeDone(unittest.TestCase):
    def test_a_worker_still_on_its_task_cannot_be_given_another(self):
        env = Env()
        self.addCleanup(env.close)
        dispatch = env.dispatch()
        task = env.task.with_name("next.md")
        task.write_text("# next\n")

        result = env.run("continue", dispatch, "--task", str(task), "--report", str(env.report.with_name("r2.md")))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not done", result.stderr)
        self.assertEqual([c for c in env.host_calls() if c[:2] == ["tmux", "send-keys"]], [])


class ClaudeTrust(unittest.TestCase):
    """dispatch --host claude trusts a new worktree only when the user already trusts an ancestor."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.config = Path(self.env.tmp.name) / "claude.json"
        self.root = self.env.cwd.parent

    def dispatch(self, host="claude"):
        self.env.dispatch(host, env={"WFC_CLAUDE_CONFIG": str(self.config)})
        return json.loads(self.config.read_text())

    def test_worktree_under_a_trusted_root_is_trusted_before_launch(self):
        self.config.write_text(json.dumps({"projects": {str(self.root): {"hasTrustDialogAccepted": True}},
                                           "otherSetting": 1}))

        config = self.dispatch()

        self.assertTrue(config["projects"][str(self.env.cwd)]["hasTrustDialogAccepted"])
        self.assertEqual(config["otherSetting"], 1)
        self.assertTrue(config["projects"][str(self.root)]["hasTrustDialogAccepted"])

    def test_worktree_without_a_trusted_ancestor_is_left_alone(self):
        untrusted = {"projects": {str(self.root): {"hasTrustDialogAccepted": False},
                                  "/somewhere/else": {"hasTrustDialogAccepted": True}}}
        self.config.write_text(json.dumps(untrusted))

        self.assertEqual(self.dispatch(), untrusted)

    def test_malformed_config_is_left_alone_and_dispatch_still_works(self):
        self.config.write_text("{not json")

        result = self.env.run("dispatch", "--host", "claude", "--task", str(self.env.task), "--cwd",
                              str(self.env.cwd), "--report", str(self.env.report),
                              env={"WFC_CLAUDE_CONFIG": str(self.config)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config.read_text(), "{not json")

    def test_config_file_mode_is_kept(self):
        self.config.write_text(json.dumps({"projects": {str(self.root): {"hasTrustDialogAccepted": True}}}))
        self.config.chmod(0o600)

        self.dispatch()

        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_trusted_root_given_through_a_symlink_is_recognised(self):
        link = Path(self.env.tmp.name) / "root-link"
        link.symlink_to(self.root)
        self.config.write_text(json.dumps({"projects": {str(link): {"hasTrustDialogAccepted": True}}}))

        config = self.dispatch()

        self.assertTrue(config["projects"][str(self.env.cwd)]["hasTrustDialogAccepted"])

    def test_other_hosts_do_not_touch_claude_trust(self):
        before = {"projects": {str(self.root): {"hasTrustDialogAccepted": True}}}
        self.config.write_text(json.dumps(before))

        self.assertEqual(self.dispatch("codex"), before)


class CodexRelay(unittest.TestCase):
    """relay pushes controller messages into a Codex controller session with codex queue."""

    THREAD = "01a1aa97-77da-7fc3-92ad-b9e7a2eb7fe4"

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.dispatch = self.env.dispatch()
        self.env.report.write_text("# report\n")

    def relay(self, seconds="1.5", env=None):
        return self.env.run("relay", "--codex-thread", self.THREAD, "--for", seconds, env=env)

    def queued(self):
        return [c for c in self.env.host_calls() if c[:2] == ["codex", "queue"]]

    def test_messages_are_queued_into_the_codex_controller_once(self):
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)

        first = self.relay()
        second = self.relay(seconds="0.8")

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        [call] = self.queued()
        self.assertEqual(call[2:4], ["--thread", self.THREAD])
        self.assertEqual(call[-1], "--resume")
        message = call[call.index("--message") + 1]
        self.assertIn('"type": "done"', message)
        self.assertIn(self.dispatch, message)

    def test_a_queue_that_exited_non_zero_is_not_retried(self):
        # A non-zero exit does not prove codex did not take the batch (it may die after accepting),
        # so the batch is logged as uncertain instead of being queued again.
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)

        result = self.relay(env={"FAKE_FAIL": "queue"})

        self.assertEqual(len([c for c in self.env.host_calls() if c[:2] == ["codex", "queue"]]), 1)
        self.assertIn("uncertain", result.stdout)
        self.assertEqual(self.env.json("wait", "--timeout", "0.5")["messages"], [])

    def test_messages_stay_waiting_when_codex_is_not_installed(self):
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)
        (self.env.bin / "codex").unlink()

        result = self.relay(env={"PATH": str(self.env.bin)})  # only the fakes: no real codex either
        [message] = self.env.json("wait", "--timeout", "1")["messages"]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr + result.stdout)
        self.assertEqual(message["type"], "done")

    def test_relay_also_runs_the_watchdog(self):
        self.env.end_session(self.dispatch)

        self.relay()

        [call] = self.queued()
        self.assertIn('"type": "exited"', call[call.index("--message") + 1])

    def test_detached_relay_outlives_its_launcher_in_its_own_session(self):
        result = self.env.run("relay", "--codex-thread", self.THREAD, "--for", "4", "--detach")
        started = json.loads(result.stdout)
        pid = started["pid"]
        self.addCleanup(lambda: subprocess.run(["kill", str(pid)], capture_output=True))

        self.assertEqual(result.returncode, 0, result.stderr)
        os.kill(pid, 0)  # still running after the launching command returned
        self.assertNotEqual(os.getsid(pid), os.getsid(0))
        self.assertEqual(Path(started["log"]).parent, self.env.state)
        child_argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        self.assertNotIn(b"--detach", child_argv)

        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)
        deadline = time.monotonic() + 3
        while not self.queued() and time.monotonic() < deadline:
            time.sleep(0.2)
        [call] = self.queued()
        self.assertIn('"type": "done"', call[call.index("--message") + 1])



class Resume(unittest.TestCase):
    """A worker whose session ended (closed by mistake, Ctrl-C, a crash) is relaunched into its own
    host conversation, so it keeps the context of every earlier round."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def launch(self, dispatch):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "new-session"]
                and c[c.index("-s") + 1] == f"wfc-{dispatch}"]

    def codex_rollout(self, dispatch, session_id, subagent=False):
        day = self.env.bin.parent / "codex-home" / "sessions" / "2026" / "11" / "06"
        day.mkdir(parents=True, exist_ok=True)
        source = {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}} if subagent else "cli"
        meta = {"type": "session_meta", "payload": {"id": session_id, "source": source}}
        text = f"You are a worker dispatched by the controller (dispatch {dispatch}). Read ..."
        prompt = {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                        "content": [{"type": "input_text", "text": text}]}}
        (day / f"rollout-{session_id}.jsonl").write_text(json.dumps(meta) + "\n" + json.dumps(prompt) + "\n")

    def opencode_session(self, dispatch, session_id):
        db = sqlite3.connect(self.env.bin.parent / "opencode.db")
        db.executescript("CREATE TABLE IF NOT EXISTS session (id TEXT, parent_id TEXT, time_created INTEGER);"
                         "CREATE TABLE IF NOT EXISTS part (id TEXT, session_id TEXT, data TEXT);")
        brief = f"You are a worker dispatched by the controller (dispatch {dispatch}). Read ..."
        db.execute("INSERT INTO session VALUES (?, NULL, 2)", (session_id,))
        db.execute("INSERT INTO part VALUES ('p1', ?, ?)", (session_id, json.dumps({"type": "text", "text": brief})))
        db.execute("INSERT INTO session VALUES ('ses_child', ?, 3)", (session_id,))
        db.execute("INSERT INTO part VALUES ('p2', 'ses_child', ?)", (json.dumps({"type": "text", "text": brief}),))
        # A controller session that only quoted the brief, created earlier, must not be picked.
        db.execute("INSERT INTO session VALUES ('ses_controller', NULL, 1)")
        db.execute("INSERT INTO part VALUES ('p3', 'ses_controller', ?)",
                   (json.dumps({"type": "text", "text": f"[router] stalled screen: {brief}"}),))
        db.commit()
        db.close()

    def test_claude_worker_is_given_a_session_id_and_resumed_into_it(self):
        dispatch = self.env.dispatch("claude")
        [first] = self.launch(dispatch)
        session_id = first[first.index("--session-id") + 1]
        self.env.end_session(dispatch)

        self.env.json("resume", dispatch)

        [_, again] = self.launch(dispatch)
        self.assertEqual(again[again.index("--resume") + 1], session_id)
        self.assertIn(str(self.env.report), again[again.index("claude") + 1])

    def test_codex_worker_is_resumed_into_its_rollout_not_a_subagent(self):
        dispatch = self.env.dispatch("codex")
        self.codex_rollout(dispatch, "sub-1111", subagent=True)
        self.codex_rollout(dispatch, "main-2222")
        self.env.end_session(dispatch)

        self.env.json("resume", dispatch)

        [_, again] = self.launch(dispatch)
        codex = again[again.index("codex"):]
        self.assertEqual(codex[1], "resume")
        self.assertIn("main-2222", codex)
        self.assertNotIn("sub-1111", codex)
        self.assertEqual(codex[codex.index("-s") + 1], "workspace-write")

    def test_opencode_worker_is_resumed_into_its_session_with_its_model(self):
        self.env.screen.write_text("> ready\n")
        dispatch = self.env.dispatch("opencode", extra_args=("--model", "deepseek/v4"))
        self.opencode_session(dispatch, "ses_main")
        self.env.end_session(dispatch)

        self.env.json("resume", dispatch, env={"FAKE_SCREEN": str(self.env.screen)}, timeout=20)

        [_, again] = self.launch(dispatch)
        opencode = again[again.index("opencode"):]
        self.assertEqual(opencode[opencode.index("-s") + 1], "ses_main")
        self.assertEqual(opencode[opencode.index("-m") + 1], "deepseek/v4")
        # OpenCode ignores --prompt on a reopened session, so the brief is typed in once it settles.
        self.assertNotIn("--prompt", opencode)
        typed = [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]
        self.assertIn(str(self.env.report), typed[-1][-1])

    def test_a_resumed_worker_is_watched_again(self):
        dispatch = self.env.dispatch("claude")
        self.env.end_session(dispatch)
        [exited] = self.env.json("wait", "--timeout", "2", "--stall-after", "60")["messages"]
        self.env.json("resume", dispatch)
        self.env.end_session(dispatch)

        [again] = self.env.json("wait", "--timeout", "2", "--stall-after", "60")["messages"]

        self.assertEqual((exited["type"], again["type"]), ("exited", "exited"))

    def test_a_finished_worker_resumes_into_its_next_task(self):
        dispatch = self.env.dispatch("claude")
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        self.env.json("wait", "--timeout", "2")
        self.env.end_session(dispatch)
        task2 = self.env.task.with_name("113-impl-r2.md")
        task2.write_text("# fix\n")
        report2 = self.env.report.with_name("r2.md")

        self.env.json("resume", dispatch, "--task", str(task2), "--report", str(report2))
        report2.write_text("# r2\n")
        self.env.run("done", "--report", str(report2), dispatch=dispatch)

        [_, again] = self.launch(dispatch)
        self.assertIn(str(task2), again[again.index("claude") + 1])
        [done] = self.env.json("wait", "--timeout", "2")["messages"]
        self.assertEqual(done["report"], str(report2))

    def test_a_finished_worker_needs_a_next_task_to_resume(self):
        dispatch = self.env.dispatch("claude")
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        self.env.end_session(dispatch)

        result = self.env.run("resume", dispatch)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--task", result.stderr)

    def test_a_worker_still_on_its_task_cannot_be_resumed_into_another(self):
        dispatch = self.env.dispatch("claude")
        self.env.end_session(dispatch)

        result = self.env.run("resume", dispatch, "--task", str(self.env.task),
                              "--report", str(self.env.report.with_name("r2.md")))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not done", result.stderr)
        self.assertEqual(len(self.launch(dispatch)), 1)

    def test_codex_controller_that_only_quoted_the_brief_is_not_resumed(self):
        dispatch = self.env.dispatch("codex")
        day = self.env.bin.parent / "codex-home" / "sessions" / "2026" / "11" / "06"
        day.mkdir(parents=True, exist_ok=True)
        quote = f"[router] Messages for the controller: You are a worker dispatched by the controller (dispatch {dispatch})"
        lines = [{"type": "session_meta", "payload": {"id": "ctl-9999", "source": "cli"}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                       "content": [{"type": "input_text", "text": quote}]}}]
        (day / "rollout-z-ctl-9999.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
        self.codex_rollout(dispatch, "main-2222")
        self.env.end_session(dispatch)

        self.env.json("resume", dispatch)

        [_, again] = self.launch(dispatch)
        self.assertIn("main-2222", again)
        self.assertNotIn("ctl-9999", again)

    def test_a_live_session_is_not_resumed(self):
        dispatch = self.env.dispatch("claude")

        result = self.env.run("resume", dispatch)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still running", result.stderr)
        self.assertEqual(len(self.launch(dispatch)), 1)

    def test_a_worker_whose_conversation_cannot_be_found_is_not_relaunched(self):
        dispatch = self.env.dispatch("codex")
        self.env.end_session(dispatch)

        result = self.env.run("resume", dispatch)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conversation", result.stderr)
        self.assertEqual(len(self.launch(dispatch)), 1)



class Races(unittest.TestCase):
    """Interleavings found by the 2026-11-07 full review (W1, W2, W4, W7, W8)."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def pause_has_session(self):
        """Make the fake tmux's has-session block on a marker and then report "gone",
        so a test can act while the watchdog holds a stale sample."""
        fake = self.env.bin / "tmux"
        fake.write_text(fake.read_text().replace(
            "    sys.exit(0 if os.path.exists",
            "    if os.environ.get('WATCH_PAUSE') and not os.path.exists(os.environ['WATCH_PAUSE']):\n"
            "        open(os.environ['WATCH_PAUSE'], 'w').close()\n"
            "        while not os.path.exists(os.environ['WATCH_PAUSE'] + '.release'): time.sleep(.01)\n"
            "        sys.exit(1)\n"
            "    sys.exit(0 if os.path.exists", 1))
        return self.env.bin.parent / "paused"

    def test_a_stale_watchdog_sample_does_not_report_a_resumed_worker_as_exited(self):
        dispatch = self.env.dispatch("claude")
        self.env.end_session(dispatch)
        marker = self.pause_has_session()
        env = {**os.environ, "PATH": f"{self.env.bin}{os.pathsep}{os.environ['PATH']}",
               "WFC_STATE": str(self.env.state), "WATCH_PAUSE": str(marker)}
        waiter = subprocess.Popen([sys.executable, str(ROUTER), "wait", "--timeout", "0"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.addCleanup(lambda: waiter.poll() is None and waiter.kill())
        for _ in range(500):
            if marker.exists():
                break
            time.sleep(0.01)
        self.env.json("resume", dispatch)
        Path(f"{marker}.release").touch()
        waiter.communicate(timeout=10)

        db = sqlite3.connect(self.env.state / "router.db")
        types = [t for (t,) in db.execute("SELECT type FROM message ORDER BY id")]
        db.close()
        self.assertEqual(types, ["resumed"])
        self.env.end_session(dispatch)
        [exited] = self.env.json("wait", "--timeout", "2", "--stall-after", "60")["messages"]
        self.assertEqual(exited["type"], "exited")  # the resumed worker is still watched

    def test_two_concurrent_replies_type_exactly_one_answer(self):
        import concurrent.futures
        dispatch = self.env.dispatch()
        question = self.env.json("ask", "Red or blue?", dispatch=dispatch)["message"]

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda a: self.env.run("reply", str(question), a), ["blue", "red"]))

        self.assertEqual(sorted(r.returncode for r in results), [0, 1])
        typed = [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"]]
        self.assertEqual(len(typed), 2)  # one answer line and its Enter

    def test_parallel_claude_dispatches_keep_every_trust_entry(self):
        import concurrent.futures
        root = self.env.bin.parent
        config = root / "claude.json"
        config.write_text(json.dumps({"projects": {str(root): {"hasTrustDialogAccepted": True}}}))
        worktrees = [root / f"wt{i}" for i in range(6)]
        for wt in worktrees:
            wt.mkdir()
        env = {"WFC_CLAUDE_CONFIG": str(config)}

        def dispatch(wt):
            return self.env.run("dispatch", "--host", "claude", "--task", str(self.env.task), "--cwd", str(wt),
                                "--report", str(self.env.report), env=env)

        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            results = list(pool.map(dispatch, worktrees))

        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        projects = json.loads(config.read_text())["projects"]
        self.assertTrue(all(projects.get(str(wt.resolve()), {}).get("hasTrustDialogAccepted") for wt in worktrees))

    def test_dispatch_without_tmux_fails_cleanly_and_leaves_no_record(self):
        python_only = self.env.bin.parent / "python-only"
        python_only.mkdir()
        (python_only / "python3").symlink_to(sys.executable)

        result = self.env.run("dispatch", "--host", "codex", "--task", str(self.env.task), "--cwd",
                              str(self.env.cwd), "--report", str(self.env.report), env={"PATH": str(python_only)})

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("tmux", result.stderr)
        db = sqlite3.connect(self.env.state / "router.db")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0], 0)
        db.close()

    def test_wait_without_tmux_reports_instead_of_crashing(self):
        dispatch = self.env.dispatch()
        python_only = self.env.bin.parent / "python-only"
        python_only.mkdir()

        result = self.env.run("wait", "--timeout", "1", env={"PATH": str(python_only)})

        self.assertNotIn("Traceback", result.stderr)
        [message] = json.loads(result.stdout)["messages"]
        self.assertEqual((message["type"], message["dispatch"]), ("exited", dispatch))


class PartialTyping(unittest.TestCase):
    """The answer text reached the TUI but its Enter did not: typing it again would double it."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        fake = self.env.bin / "tmux"
        # The first Enter fails; everything else works.
        fake.write_text(fake.read_text().replace(
            "if os.environ.get('FAKE_FAIL') == sys.argv[1]:",
            "flag = os.path.join(os.path.dirname(sys.argv[0]), '.enter-failed')\n"
            "if sys.argv[1] == 'send-keys' and sys.argv[-1] == 'Enter' and os.environ.get('FAKE_ENTER_FAIL') and not os.path.exists(flag):\n"
            "    open(flag, 'w').close()\n"
            "    sys.exit(1)\n"
            "if os.environ.get('FAKE_FAIL') == sys.argv[1]:", 1))

    def test_a_reply_whose_enter_failed_is_not_typed_again(self):
        dispatch = self.env.dispatch()
        question = self.env.json("ask", "Red or blue?", dispatch=dispatch)["message"]
        env = {"FAKE_ENTER_FAIL": "1"}

        first = self.env.run("reply", str(question), "blue", env=env)
        second = self.env.run("reply", str(question), "blue", env=env)

        self.assertNotEqual(first.returncode, 0)
        self.assertIn("attach", first.stderr)
        self.assertNotEqual(second.returncode, 0)
        texts = [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]
        self.assertEqual(len(texts), 1)

    def test_a_reply_whose_text_never_arrived_can_be_retried(self):
        dispatch = self.env.dispatch()
        question = self.env.json("ask", "Red or blue?", dispatch=dispatch)["message"]

        first = self.env.run("reply", str(question), "blue", env={"FAKE_FAIL": "send-keys"})
        second = self.env.run("reply", str(question), "blue")

        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0, second.stderr)


class RelayUncertainDelivery(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_a_timed_out_queue_is_not_retried(self):
        fake = self.env.bin / "codex"
        fake.write_text(fake.read_text() + "if sys.argv[1] == 'queue': time.sleep(5)\n")
        dispatch = self.env.dispatch()
        self.env.report.write_text("# r\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)

        result = self.env.run("relay", "--codex-thread", "t-1", "--for", "4", env={"WFC_QUEUE_TIMEOUT": "1"},
                              timeout=20)

        queued = [c for c in self.env.host_calls() if c[:2] == ["codex", "queue"]]
        self.assertEqual(len(queued), 1)
        self.assertIn("uncertain", result.stdout)
        self.assertEqual(self.env.json("wait", "--timeout", "0.5")["messages"], [])


if __name__ == "__main__":
    unittest.main()
