"""Tests for router.py through its command-line interface."""

import calendar
import fcntl
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
        root = Path(self.tmp.name).resolve()  # macOS: /var is a symlink to /private/var
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
        # On an Enter, FAKE_ENTER_SCREEN's file replaces FAKE_SCREEN's (what the TUI drew in answer);
        # FAKE_ENTER_SCREENS (paths joined by os.pathsep) does so per Enter: the nth Enter draws the
        # nth file, the last one from then on. FAKE_SCREEN_ALT makes every other capture print that file.
        # and FAKE_ENTER_SQL runs against router.db (something that happened meanwhile).
        # The fake tmux keeps one marker file per session: new-session creates it (after
        # FAKE_LAUNCH_DELAY seconds) and has-session checks it. capture-pane prints FAKE_SCREEN's
        # file if set, else a fresh screen each time (a busy worker).
        # The marker holds the session's environment (new-session's -e), which show-environment
        # prints; kill-session removes it, and fails for FAKE_KILL_FAIL=<session> or a missing one.
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
                f"    session_env = dict(sys.argv[i + 1].split('=', 1) for i, a in enumerate(sys.argv) if a == '-e')\n"
                f"    open(os.path.join(sessions, sys.argv[sys.argv.index('-s') + 1]), 'w').write(json.dumps(session_env))\n"
                f"if {name!r} == 'tmux' and sys.argv[1] == 'has-session':\n"
                f"    sys.exit(0 if os.path.exists(os.path.join(sessions, sys.argv[sys.argv.index('-t') + 1])) else 1)\n"
                f"if {name!r} == 'tmux' and sys.argv[1:2] == ['send-keys'] and sys.argv[-1] == 'Enter':\n"
                f"    if os.environ.get('FAKE_ENTER_SCREEN'):\n"
                f"        open(os.environ['FAKE_SCREEN'], 'w').write(open(os.environ['FAKE_ENTER_SCREEN']).read())\n"
                f"    if os.environ.get('FAKE_ENTER_SCREENS'):\n"
                f"        counter = os.environ['FAKE_SCREEN'] + '.enters'\n"
                f"        n = int(open(counter).read()) if os.path.exists(counter) else 0\n"
                f"        open(counter, 'w').write(str(n + 1))\n"
                f"        screens = os.environ['FAKE_ENTER_SCREENS'].split(os.pathsep)\n"
                f"        open(os.environ['FAKE_SCREEN'], 'w').write(open(screens[min(n, len(screens) - 1)]).read())\n"
                f"    if os.environ.get('FAKE_ENTER_SQL'):\n"
                f"        import sqlite3; db = sqlite3.connect(os.path.join(os.environ['WFC_STATE'], 'router.db'))\n"
                f"        db.executescript(os.environ['FAKE_ENTER_SQL']); db.close()\n"
                f"if {name!r} == 'tmux' and sys.argv[1] == 'show-environment':\n"
                f"    marker = os.path.join(sessions, sys.argv[sys.argv.index('-t') + 1])\n"
                f"    if not os.path.exists(marker):\n"
                f"        sys.exit(1)\n"
                f"    text = open(marker).read()\n"
                f"    value = (json.loads(text) if text else {{}}).get(sys.argv[-1])\n"
                f"    if value is None:\n"
                f"        print('unknown variable: ' + sys.argv[-1], file=sys.stderr); sys.exit(1)\n"
                f"    print(sys.argv[-1] + '=' + value)\n"
                f"if {name!r} == 'tmux' and sys.argv[1] == 'kill-session':\n"
                f"    target = sys.argv[sys.argv.index('-t') + 1]\n"
                f"    if os.environ.get('FAKE_KILL_FAIL') == target or not os.path.exists(os.path.join(sessions, target)):\n"
                f"        sys.exit(1)\n"
                f"    os.unlink(os.path.join(sessions, target))\n"
                f"if {name!r} == 'tmux' and sys.argv[1] == 'capture-pane':\n"
                f"    screen = os.environ.get('FAKE_SCREEN')\n"
                f"    if screen and os.environ.get('FAKE_SCREEN_ALT'):\n"
                f"        counter = screen + '.captures'\n"
                f"        n = int(open(counter).read()) if os.path.exists(counter) else 0\n"
                f"        open(counter, 'w').write(str(n + 1))\n"
                f"        screen = os.environ['FAKE_SCREEN_ALT'] if n % 2 else screen\n"
                f"    print(open(screen).read() if screen else f'Working ({{time.time_ns()}})')\n")
            fake.chmod(0o755)

    def end_session(self, dispatch):
        (self.sessions / f"wfc-{dispatch}").unlink()

    def run(self, *args, dispatch=None, timeout=10, env=None, cwd=None):
        return subprocess.run([sys.executable, str(ROUTER), *args], capture_output=True,
                              text=True, timeout=timeout, env=self.router_env(dispatch, env), cwd=cwd)

    def router_env(self, dispatch=None, env=None):
        # Tests never read or write the real ~/.claude.json.
        full_env = {**os.environ, "WFC_STATE": str(self.state),
                    "WFC_CLAUDE_CONFIG": str(self.bin.parent / "no-claude-config.json"),
                    "CODEX_HOME": str(self.bin.parent / "codex-home"),
                    "WFC_OPENCODE_DB": str(self.bin.parent / "opencode.db"),
                    "CLAUDE_CONFIG_DIR": str(self.bin.parent / "claude-home"),
                    # Short read-only checks keep the suite fast; ReadOnlySessions sets its own.
                    "WFC_TYPED_CHECK_SECONDS": "0.2", "WFC_RESUME_CHECK_SECONDS": "0.3",
                    "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", **(env or {})}
        full_env.pop("WFC_DISPATCH", None)
        if "CODEX_THREAD_ID" not in (env or {}):
            full_env.pop("CODEX_THREAD_ID", None)  # set when the tests themselves run inside Codex
        if dispatch:
            full_env["WFC_DISPATCH"] = dispatch
        return full_env

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

    def codex_controller(self, thread, subagent=False, meta_id=None):
        """A Codex controller's rollout, named the way Codex names it, so relay accepts `thread`."""
        day = self.bin.parent / "codex-home" / "sessions" / "2026" / "11" / "09"
        day.mkdir(parents=True, exist_ok=True)
        source = {"subagent": {"thread_spawn": {"parent_thread_id": "p", "depth": 1}}} if subagent else "cli"
        meta = {"type": "session_meta", "payload": {"id": meta_id or thread, "source": source}}
        rollout = day / f"rollout-2026-11-09T06-25-37-{thread}.jsonl"
        rollout.write_text(json.dumps(meta) + "\n")
        return rollout


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

    def test_stable_done_receipt_is_idempotent_and_ack_is_versioned(self):
        dispatch = self.env.dispatch()
        self.env.report.write_text("# report\n")
        first = self.env.run("done", "--report", str(self.env.report), "--message-id", "receipt-1", dispatch=dispatch)
        self.assertEqual(first.returncode, 0, first.stderr)
        first_json = json.loads(first.stdout)
        delivered = self.env.json("wait", "--timeout", "2")["messages"]
        self.assertEqual(delivered[0]["messageId"], first_json["messageId"])
        self.assertEqual(delivered[0]["state"], "received")
        duplicate = self.env.run("done", "--report", str(self.env.report), "--message-id", "receipt-1", dispatch=dispatch)
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(json.loads(duplicate.stdout)["messageId"], first_json["messageId"])
        self.assertEqual(self.env.json("wait", "--timeout", "0.1"), {"messages": [], "timedOut": True})
        ack = self.env.json("ack", first_json["messageId"])
        self.assertEqual((ack["state"], ack["statusVersion"], ack["duplicate"]), ("processed", 3, False))
        again = self.env.json("ack", first_json["messageId"])
        self.assertTrue(again["duplicate"])

    def test_stable_receipt_with_different_content_is_rejected(self):
        dispatch = self.env.dispatch()
        first = self.env.run("ask", "first", "--message-id", "receipt-2", dispatch=dispatch)
        self.assertEqual(first.returncode, 0, first.stderr)
        result = self.env.run("ask", "second", "--message-id", "receipt-2", dispatch=dispatch)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("conflicts", result.stderr)


def make_repo(main, worktree=None):
    """A git repository at `main` with one commit and, if given, a linked worktree at `worktree`."""
    git = lambda *a: subprocess.run(["git", "-C", str(main), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                                     *a], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", str(main)], check=True, capture_output=True)
    git("commit", "-q", "--allow-empty", "-m", "one")
    if worktree:
        git("worktree", "add", "-q", str(worktree))


class GitOption(unittest.TestCase):
    """dispatch --git adds the worktree's git directories to a Codex worker's writable dirs, so it
    can commit in its sandbox (docs/probes-20261109-codex-git.md)."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def launches(self):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "new-session"]]

    def added(self, call):
        return [call[i + 1] for i, arg in enumerate(call) if arg == "--add-dir"]

    def test_a_linked_worktree_gets_its_git_dir_and_the_common_dir(self):
        main = self.env.cwd.parent / "main"
        self.env.cwd = self.env.cwd.parent / "trees" / "v1"
        make_repo(main, self.env.cwd)

        self.env.dispatch("codex", extra_args=["--git"])

        [call] = self.launches()
        added = self.added(call[call.index("codex"):])
        self.assertIn(str(main / ".git" / "worktrees" / "v1"), added)
        self.assertIn(str(main / ".git"), added)
        self.assertTrue(self.env.json("status")["dispatches"][0]["git"])

    def test_a_main_checkout_gets_its_git_dir_once_as_an_absolute_path(self):
        make_repo(self.env.cwd)

        self.env.dispatch("codex", extra_args=["--git"])

        [call] = self.launches()
        added = self.added(call[call.index("codex"):])
        self.assertEqual([d for d in added if d.endswith(".git")], [str(self.env.cwd / ".git")])

    def test_without_git_no_git_directory_is_added(self):
        make_repo(self.env.cwd)

        self.env.dispatch("codex")

        [call] = self.launches()
        self.assertFalse([d for d in self.added(call) if ".git" in d])
        self.assertFalse(self.env.json("status")["dispatches"][0]["git"])

    def test_outside_a_repository_git_is_refused_and_leaves_no_dispatch(self):
        result = self.env.run("dispatch", "--host", "codex", "--task", str(self.env.task), "--cwd", str(self.env.cwd),
                              "--report", str(self.env.report), "--git",
                              env={"GIT_CEILING_DIRECTORIES": str(self.env.cwd.parent)})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a git repository", result.stderr)  # refused by --git, not by argparse
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.launches(), [])
        db = sqlite3.connect(self.env.state / "router.db")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0], 0)
        db.close()

    def test_git_is_ignored_with_a_note_for_hosts_that_can_write_git_anyway(self):
        make_repo(self.env.cwd)
        for host in ("claude", "opencode"):
            with self.subTest(host=host):
                self.env.calls.unlink(missing_ok=True)
                out = self.env.json("dispatch", "--host", host, "--task", str(self.env.task), "--cwd",
                                    str(self.env.cwd), "--report", str(self.env.report), "--git")

                [call] = self.launches()
                self.assertFalse([a for a in call if a.endswith(".git")])
                self.assertIn("--git", " ".join(out["ignored"]))

    def test_resume_keeps_git(self):
        make_repo(self.env.cwd)
        dispatch = self.env.dispatch("codex", extra_args=["--git"])
        Resume.codex_rollout(self, dispatch, "main-4444")
        self.env.end_session(dispatch)

        self.env.json("resume", dispatch)

        [_, again] = self.launches()
        self.assertIn(str(self.env.cwd / ".git"), self.added(again[again.index("codex"):]))


def make_non_utf8_path(parent, name=b"caf\xe9"):
    """Create a directory whose name is not valid UTF-8 (POSIX: any bytes but / and NUL),
    or skip the test where the file system refuses one (macOS APFS does: EILSEQ)."""
    path = os.path.join(os.fsencode(parent), name)
    try:
        os.mkdir(path)
    except OSError as err:
        raise unittest.SkipTest(f"this file system cannot create a non-UTF-8 file name: {err}")
    return path


class Labels(unittest.TestCase):
    """Each dispatch has a label, `<project> | <ticket> | <role>`, shown in status, as the tmux window
    name and, for Claude Code, as the session name."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def launches(self):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "new-session"]]

    def window_name(self, call):
        return call[call.index("-n") + 1]

    def test_default_label_names_the_main_repository_and_the_report_directory(self):
        root = self.env.cwd.parent
        main = root / "myproj"
        git = lambda *a, cwd=main: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c",
                                                   "user.email=t@example.invalid", *a], check=True, capture_output=True)
        git("init", "-q", str(main), cwd=root)
        git("commit", "-q", "--allow-empty", "-m", "one")
        self.env.cwd = root / "trees" / "v1"
        git("worktree", "add", "-q", str(self.env.cwd))
        self.env.report = root / "reports" / "113-impl-fix" / "r1.md"

        dispatch = self.env.dispatch("claude")

        label = "myproj | 113-impl | fix"
        [call] = self.launches()
        self.assertEqual(self.window_name(call), label)
        self.assertEqual(call[call.index(f"wfc-{dispatch}") - 1], "-s")  # the session name is unchanged
        claude = call[call.index("claude"):]
        self.assertEqual(claude[claude.index("--name") + 1], label)
        [view] = self.env.json("status")["dispatches"]
        self.assertEqual(view["label"], label)

    def test_outside_git_the_project_is_the_worktree_and_an_unsplittable_directory_is_kept_whole(self):
        self.env.dispatch("codex", env={"GIT_CEILING_DIRECTORIES": str(self.env.cwd.parent)})

        [call] = self.launches()
        self.assertEqual(self.window_name(call), "worktree | reports")

    def test_label_overrides_the_default_and_loses_control_characters(self):
        self.env.dispatch("opencode", extra_args=("--label", " app |\n 7 |\tdesign\x1b "))

        [call] = self.launches()
        self.assertEqual(self.window_name(call), "app | 7 | design")
        self.assertEqual(self.env.json("status")["dispatches"][0]["label"], "app | 7 | design")

    def test_a_main_repository_with_a_non_utf8_name_still_gives_a_default_label(self):
        root = self.env.cwd.parent
        main = make_non_utf8_path(root)  # git prints it raw: the common dir of a linked worktree is absolute
        git = lambda *a: subprocess.run([b"git", b"-C", main, b"-c", b"user.name=t", b"-c",
                                         b"user.email=t@example.invalid", *a], check=True, capture_output=True)
        git(b"init", b"-q")
        git(b"commit", b"-q", b"--allow-empty", b"-m", b"one")
        self.env.cwd = root / "trees" / "v1"
        git(b"worktree", b"add", b"-q", os.fsencode(self.env.cwd))
        self.env.report = root / "reports" / "113-impl" / "r1.md"

        out = self.env.json("dispatch", "--host", "codex", "--task", str(self.env.task), "--cwd",
                            str(self.env.cwd), "--report", str(self.env.report))

        self.assertEqual(out["label"], "caf\ufffd | 113 | impl")  # undecodable bytes read as U+FFFD
        [call] = self.launches()
        self.assertEqual(self.window_name(call), "caf\ufffd | 113 | impl")

    def test_a_label_that_cleans_to_nothing_falls_back_to_the_default(self):
        self.env.dispatch("codex", extra_args=("--label", " \n\t\x1b "),
                          env={"GIT_CEILING_DIRECTORIES": str(self.env.cwd.parent)})

        [call] = self.launches()
        self.assertEqual(self.window_name(call), "worktree | reports")

    def test_resume_keeps_the_label(self):
        dispatch = self.env.dispatch("claude", extra_args=("--label", "p | 9 | review"))
        self.env.end_session(dispatch)

        self.env.json("resume", dispatch)

        [_, again] = self.launches()
        self.assertEqual(self.window_name(again), "p | 9 | review")
        claude = again[again.index("claude"):]
        self.assertEqual(claude[claude.index("--name") + 1], "p | 9 | review")


class DoneHead(unittest.TestCase):
    """done records the worker's HEAD and whether its worktree is dirty, read in the dispatch's cwd."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.report.write_text("# report\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.env.cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               *args], capture_output=True, text=True, check=True).stdout.strip()

    def done(self, dispatch, **kw):
        # Run from elsewhere: the dispatch's cwd counts, not the directory `done` runs in.
        result = self.env.run("done", "--report", str(self.env.report), dispatch=dispatch,
                              cwd=self.env.state.parent, **kw)
        self.assertEqual(result.returncode, 0, result.stderr)
        [message] = self.env.json("wait", "--timeout", "2")["messages"]
        return message

    def test_head_of_a_clean_worktree_is_recorded_and_shown_by_status(self):
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "one")
        dispatch = self.env.dispatch()

        message = self.done(dispatch)

        self.assertEqual((message["head"], message["dirty"]), (self.git("rev-parse", "HEAD"), False))
        [view] = self.env.json("status")["dispatches"]
        self.assertEqual((view["last"]["head"], view["last"]["dirty"]), (message["head"], False))

    def test_an_untracked_file_makes_the_worktree_dirty(self):
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "one")
        (self.env.cwd / "new.txt").write_text("x\n")
        dispatch = self.env.dispatch()

        self.assertEqual(self.done(dispatch)["dirty"], True)

    def test_outside_a_git_repository_both_are_null(self):
        dispatch = self.env.dispatch()

        message = self.done(dispatch, env={"GIT_CEILING_DIRECTORIES": str(self.env.cwd.parent)})

        self.assertEqual((message["head"], message["dirty"]), (None, None))

    def test_an_untracked_non_utf8_file_name_printed_raw_makes_the_worktree_dirty(self):
        self.git("init", "-q")
        self.git("config", "core.quotePath", "false")  # git status prints the name's bytes as they are
        self.git("commit", "-q", "--allow-empty", "-m", "one")
        make_non_utf8_path(self.env.cwd, b"r\xe9sum\xe9")
        open(os.path.join(make_non_utf8_path(self.env.cwd, b"\xff\xfe"), b"f"), "wb").close()
        dispatch = self.env.dispatch()

        message = self.done(dispatch)

        self.assertEqual((message["head"], message["dirty"]), (self.git("rev-parse", "HEAD"), True))

    def test_a_git_that_fails_or_hangs_does_not_stop_done(self):
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "one")
        dispatch = self.env.dispatch()
        fakes = self.env.bin.parent / "fake-git"
        fakes.mkdir()
        for mode in ("fail", "hang"):
            with self.subTest(mode=mode):
                fake = fakes / "git"
                fake.write_text(f"#!{sys.executable}\nimport sys, time\n"
                                + ("sys.exit(128)\n" if mode == "fail" else "time.sleep(60)\n"))
                fake.chmod(0o755)
                path = f"{fakes}{os.pathsep}{self.env.bin}{os.pathsep}{os.environ['PATH']}"

                message = self.done(dispatch, env={"PATH": path}, timeout=30)

                self.assertEqual((message["head"], message["dirty"]), (None, None))


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

    def test_effort_is_passed_to_each_host_in_its_own_flag(self):
        for host, flag, value in (("codex", "-c", "model_reasoning_effort=high"), ("claude", "--effort", "high"),
                                  ("opencode", "--variant", "high")):
            with self.subTest(host=host):
                self.env.calls.unlink(missing_ok=True)
                self.env.dispatch(host, extra_args=["--effort", "high"])

                [call] = self.env.host_calls()
                command = call[call.index(host):]
                self.assertEqual(command[command.index(flag) + 1], value)

    def test_network_opens_the_codex_sandbox_to_the_network(self):
        self.env.dispatch("codex")
        self.env.calls.unlink()
        self.env.dispatch("codex", extra_args=["--network"])

        [call] = self.env.host_calls()
        codex = call[call.index("codex"):]
        self.assertEqual(codex[codex.index("-c") + 1], "sandbox_workspace_write.network_access=true")

    def test_without_effort_or_network_codex_gets_no_config_override(self):
        self.env.dispatch("codex")

        [call] = self.env.host_calls()
        self.assertNotIn("-c", call[call.index("codex"):])

    def test_network_is_ignored_with_a_note_for_hosts_that_are_online_anyway(self):
        for host in ("claude", "opencode"):
            with self.subTest(host=host):
                self.env.calls.unlink(missing_ok=True)
                out = self.env.json("dispatch", "--host", host, "--task", str(self.env.task), "--cwd",
                                    str(self.env.cwd), "--report", str(self.env.report), "--network")

                [call] = self.env.host_calls()
                self.assertNotIn("network", " ".join(call[call.index(host):-1]))
                self.assertIn("--network", out["ignored"][0])

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

    def test_a_failed_text_send_to_a_live_session_fails_cleanly_and_is_not_retried(self):
        # The session is still there, so the text may have arrived: retrying could double the answer.
        failed = self.env.run("reply", str(self.question["id"]), "blue", env={"FAKE_FAIL": "send-keys"})
        retried = self.env.run("reply", str(self.question["id"]), "blue")

        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn("Traceback", failed.stderr)
        self.assertIn("may or may not", failed.stderr)
        self.assertNotEqual(retried.returncode, 0)

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
        self.env.codex_controller(self.THREAD)

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
        # `ps` reads a process's arguments on Linux and macOS alike (macOS has no /proc).
        child_args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True).stdout
        self.assertIn("relay", child_args)
        self.assertNotIn("--detach", child_args)

        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)
        deadline = time.monotonic() + 3
        while not self.queued() and time.monotonic() < deadline:
            time.sleep(0.2)
        [call] = self.queued()
        self.assertIn('"type": "done"', call[call.index("--message") + 1])



class CodexRelayThread(unittest.TestCase):
    """relay checks the Codex thread before it starts: `codex queue` also succeeds for a thread
    that does not exist, so a wrong id would silently deliver nothing."""

    THREAD = CodexRelay.THREAD
    OTHER = "01a1c045-6cbc-7130-9daa-d087275fb216"

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def relay(self, *args, env=None):
        return self.env.run("relay", *args, "--for", "0.5", env=env)

    def queued(self):
        return [c for c in self.env.host_calls() if c[:2] == ["codex", "queue"]]

    def assert_refused(self, result, *words):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        for word in words:
            self.assertIn(word, result.stderr)
        self.assertEqual(self.queued(), [])

    def test_codex_flag_relays_to_the_current_codex_thread(self):
        self.env.codex_controller(self.THREAD)
        dispatch = self.env.dispatch()
        self.env.report.write_text("# r\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)

        result = self.relay("--codex", env={"CODEX_THREAD_ID": self.THREAD})

        self.assertEqual(result.returncode, 0, result.stderr)
        [call] = self.queued()
        self.assertEqual(call[2:4], ["--thread", self.THREAD])

    def test_codex_flag_outside_codex_is_refused(self):
        self.assert_refused(self.relay("--codex"), "CODEX_THREAD_ID")

    def test_a_thread_without_a_rollout_is_refused(self):
        self.env.codex_controller(self.OTHER)
        self.assert_refused(self.relay("--codex-thread", self.THREAD), self.THREAD, "no rollout")

    def test_a_sub_agent_thread_is_refused(self):
        self.env.codex_controller(self.THREAD, subagent=True)
        self.assert_refused(self.relay("--codex-thread", self.THREAD), "sub-agent")
        self.assert_refused(self.relay("--codex", env={"CODEX_THREAD_ID": self.THREAD}), "sub-agent")

    def test_a_rollout_whose_first_line_names_another_thread_is_refused(self):
        self.env.codex_controller(self.THREAD, meta_id=self.OTHER)
        self.assert_refused(self.relay("--codex-thread", self.THREAD), "no rollout")

    def test_a_thread_name_is_refused(self):
        self.assert_refused(self.relay("--codex-thread", "controller"), "UUID")

    def test_an_explicit_thread_other_than_the_current_codex_thread_is_refused(self):
        self.env.codex_controller(self.THREAD)
        self.env.codex_controller(self.OTHER)

        self.assert_refused(self.relay("--codex-thread", self.THREAD, env={"CODEX_THREAD_ID": self.OTHER}),
                            self.THREAD, self.OTHER)
        same = self.relay("--codex-thread", self.THREAD.upper(), env={"CODEX_THREAD_ID": self.THREAD})
        self.assertEqual(same.returncode, 0, same.stderr)

    def test_codex_flag_and_an_explicit_thread_together_are_refused(self):
        self.env.codex_controller(self.THREAD)
        self.assert_refused(self.relay("--codex", "--codex-thread", self.THREAD,
                                       env={"CODEX_THREAD_ID": self.THREAD}))

    def test_a_detached_relay_with_a_bad_thread_starts_no_child(self):
        result = self.env.run("relay", "--codex-thread", self.THREAD, "--detach", "--for", "2")
        if '"pid"' in result.stdout:  # a regression must not leave a relay running
            self.addCleanup(lambda: subprocess.run(["kill", str(json.loads(result.stdout)["pid"])],
                                                   capture_output=True))

        self.assert_refused(result, "no rollout")
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.env.state / "relay.log").exists())

    def test_a_queue_that_finds_no_rollout_stops_the_relay_and_keeps_the_batch(self):
        # codex queue fails outright for a thread it has no rollout for (seen on Codex 0.167 when
        # the rollout lies outside its own CODEX_HOME): the batch certainly did not arrive.
        self.env.codex_controller(self.THREAD)
        fake = self.env.bin / "codex"
        fake.write_text(fake.read_text() + "if sys.argv[1] == 'queue':\n"
                        "    print('Error: no rollout found for thread id ' + sys.argv[3], file=sys.stderr)\n"
                        "    sys.exit(1)\n")
        dispatch = self.env.dispatch()
        self.env.report.write_text("# r\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)

        result = self.env.run("relay", "--codex-thread", self.THREAD, "--for", "3")
        [message] = self.env.json("wait", "--timeout", "1")["messages"]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no rollout found", result.stderr)
        self.assertNotIn("uncertain", result.stdout + result.stderr)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(message["type"], "done")

    def test_the_check_opens_only_the_rollout_named_by_the_thread(self):
        # Every other rollout is left unread: a FIFO here would block any reader forever.
        os.mkfifo(self.env.codex_controller(self.THREAD).with_name(f"rollout-2026-11-09T07-00-00-{self.OTHER}.jsonl"))

        result = self.relay("--codex-thread", self.THREAD)

        self.assertEqual(result.returncode, 0, result.stderr)


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

    def test_resume_keeps_the_effort_and_network_given_at_dispatch(self):
        dispatch = self.env.dispatch("codex", extra_args=("--effort", "xhigh", "--network"))
        self.codex_rollout(dispatch, "main-3333")
        self.env.end_session(dispatch)

        self.env.json("resume", dispatch)

        [_, again] = self.launch(dispatch)
        codex = again[again.index("codex"):]
        overrides = [codex[i + 1] for i, arg in enumerate(codex) if arg == "-c"]
        self.assertEqual(overrides, ["model_reasoning_effort=xhigh", "sandbox_workspace_write.network_access=true"])

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

    def test_a_reply_whose_text_send_was_interrupted_is_not_typed_again(self):
        # send-keys was killed after handing the text over: a non-zero exit is not proof it did not arrive.
        fake = self.env.bin / "tmux"
        fake.write_text(fake.read_text().replace(
            "if os.environ.get('FAKE_FAIL') == sys.argv[1]:",
            "kflag = os.path.join(os.path.dirname(sys.argv[0]), '.text-killed')\n"
            "if sys.argv[1] == 'send-keys' and '-l' in sys.argv and os.environ.get('FAKE_TEXT_KILL') and not os.path.exists(kflag):\n"
            "    open(kflag, 'w').close()\n"
            "    import signal; os.kill(os.getpid(), signal.SIGTERM)\n"
            "if os.environ.get('FAKE_FAIL') == sys.argv[1]:", 1))
        dispatch = self.env.dispatch()
        question = self.env.json("ask", "Red or blue?", dispatch=dispatch)["message"]
        env = {"FAKE_TEXT_KILL": "1"}

        first = self.env.run("reply", str(question), "blue", env=env)
        second = self.env.run("reply", str(question), "blue", env=env)

        self.assertNotEqual(first.returncode, 0)
        self.assertIn("may or may not", first.stderr)
        self.assertNotEqual(second.returncode, 0)
        texts = [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]
        self.assertEqual(len(texts), 1)

    def test_a_failed_send_is_uncertain_even_when_the_session_probe_fails_too(self):
        # Both the text send and any follow-up probe fail: nothing proves the text did not arrive.
        dispatch = self.env.dispatch()
        question = self.env.json("ask", "Red or blue?", dispatch=dispatch)["message"]
        env = {"FAKE_FAIL": "send-keys"}
        fake = self.env.bin / "tmux"
        fake.write_text(fake.read_text().replace(
            "if os.environ.get('FAKE_FAIL') == sys.argv[1]:",
            "if os.environ.get('FAKE_PROBE_FAIL') and sys.argv[1] == 'has-session':\n"
            "    sys.exit(1)\n"
            "if os.environ.get('FAKE_FAIL') == sys.argv[1]:", 1))

        first = self.env.run("reply", str(question), "blue", env={**env, "FAKE_PROBE_FAIL": "1"})
        second = self.env.run("reply", str(question), "blue")

        self.assertIn("may or may not", first.stderr)
        self.assertNotEqual(second.returncode, 0)

    def test_a_reply_whose_text_never_arrived_can_be_retried(self):
        dispatch = self.env.dispatch()
        question = self.env.json("ask", "Red or blue?", dispatch=dispatch)["message"]

        # tmux could not be run at all, so the text certainly did not arrive; once it can, the reply can go.
        python_only = self.env.bin.parent / "python-only"
        python_only.mkdir()
        first = self.env.run("reply", str(question), "blue", env={"PATH": str(python_only)})
        second = self.env.run("reply", str(question), "blue")

        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0, second.stderr)


class ResumePartialTyping(unittest.TestCase):
    def test_an_opencode_resume_whose_enter_failed_still_records_the_new_task(self):
        env = Env()
        self.addCleanup(env.close)
        fake = env.bin / "tmux"
        fake.write_text(fake.read_text().replace(
            "if os.environ.get('FAKE_FAIL') == sys.argv[1]:",
            "flag = os.path.join(os.path.dirname(sys.argv[0]), '.enter-failed')\n"
            "if sys.argv[1] == 'send-keys' and sys.argv[-1] == 'Enter' and os.environ.get('FAKE_ENTER_FAIL') and not os.path.exists(flag):\n"
            "    open(flag, 'w').close()\n"
            "    sys.exit(1)\n"
            "if os.environ.get('FAKE_FAIL') == sys.argv[1]:", 1))
        dispatch = env.dispatch("opencode", extra_args=("--model", "m"))
        Resume.opencode_session(type("R", (), {"env": env})(), dispatch, "ses_main")
        env.report.write_text("# r1\n")
        env.run("done", "--report", str(env.report), dispatch=dispatch)
        env.end_session(dispatch)
        task2 = env.task.with_name("113-impl-r2.md")
        task2.write_text("# r2\n")
        report2 = env.report.with_name("r2.md")
        env.screen.write_text("> ready\n")
        screen = {"FAKE_SCREEN": str(env.screen)}

        first = env.run("resume", dispatch, "--task", str(task2), "--report", str(report2),
                        env={**screen, "FAKE_ENTER_FAIL": "1"}, timeout=20)
        env.end_session(dispatch)
        second = env.run("resume", dispatch, env=screen, timeout=20)

        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0, second.stderr)
        typed = [c[-1] for c in env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]
        self.assertIn(str(report2), typed[-1])


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

        self.env.codex_controller(CodexRelay.THREAD)
        result = self.env.run("relay", "--codex-thread", CodexRelay.THREAD, "--for", "4", env={"WFC_QUEUE_TIMEOUT": "1"},
                              timeout=20)

        queued = [c for c in self.env.host_calls() if c[:2] == ["codex", "queue"]]
        self.assertEqual(len(queued), 1)
        self.assertIn("uncertain", result.stdout)
        self.assertEqual(self.env.json("wait", "--timeout", "0.5")["messages"], [])



class ParallelState(unittest.TestCase):
    """W10, W11 from the 2026-11-07 final review: parallel commands on one state database."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_parallel_first_use_of_an_old_state_database_upgrades_it_once(self):
        import concurrent.futures
        import threading
        self.env.state.mkdir()
        db = sqlite3.connect(self.env.state / "router.db")
        db.executescript(
            "CREATE TABLE dispatch(id TEXT PRIMARY KEY, host TEXT, cwd TEXT, task TEXT, created REAL);"
            "CREATE TABLE message(id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch TEXT, recipient TEXT,"
            " type TEXT, body TEXT, created REAL, delivered INTEGER DEFAULT 0);"
            "CREATE TABLE screen_watch(dispatch TEXT PRIMARY KEY, screen TEXT, since REAL, reported_screen TEXT);")
        db.execute("BEGIN")
        db.execute("SELECT * FROM dispatch").fetchall()  # a held read lock lines both commands up
        gate = threading.Barrier(2)

        def dispatch(_):
            gate.wait()
            return self.env.run("dispatch", "--host", "codex", "--task", str(self.env.task), "--cwd",
                                str(self.env.cwd), "--report", str(self.env.report), timeout=40)

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(dispatch, i) for i in range(2)]
            time.sleep(1)
            db.rollback()
            db.close()
            results = [f.result() for f in futures]

        self.assertEqual([r.returncode for r in results], [0, 0], [r.stderr for r in results])

    def test_two_concurrent_continues_give_the_next_task_once(self):
        import concurrent.futures
        dispatch = self.env.dispatch()
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        barrier = self.env.bin.parent / "barrier"
        barrier.mkdir()
        fake = self.env.bin / "tmux"
        # Both continues try to pass their has-session check before either records the new round;
        # the input lock lets only one in, so the barrier gives up waiting for the other after 2 s.
        fake.write_text(fake.read_text().replace(
            "    sys.exit(0 if os.path.exists",
            f"    if os.environ.get('CONTINUE_BARRIER'):\n"
            f"        open(os.path.join({str(barrier)!r}, str(os.getpid())), 'w').close()\n"
            f"        give_up = time.time() + 2\n"
            f"        while len(os.listdir({str(barrier)!r})) < 2 and time.time() < give_up: time.sleep(.01)\n"
            f"    sys.exit(0 if os.path.exists", 1))
        task = self.env.task.with_name("r2.md")
        task.write_text("# r2\n")
        report = self.env.report.with_name("r2.md")

        def next_round(_):
            return self.env.run("continue", dispatch, "--task", str(task), "--report", str(report),
                                env={"CONTINUE_BARRIER": "1"}, timeout=20)

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(next_round, range(2)))

        self.assertEqual(sorted(r.returncode for r in results), [0, 1])
        texts = [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]
        self.assertEqual(len(texts), 1)



class RoundIdentity(unittest.TestCase):
    """W12, W14, W15 from the 2026-11-07 round-6 review."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_a_retried_done_from_an_earlier_round_does_not_finish_the_current_one(self):
        dispatch = self.env.dispatch()
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        self.env.json("wait", "--timeout", "1")
        task2 = self.env.task.with_name("r2.md")
        task2.write_text("# r2\n")
        self.env.json("continue", dispatch, "--task", str(task2), "--report", str(self.env.report.with_name("r2.md")))

        stale = self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)

        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("earlier round", stale.stderr)
        self.assertEqual(self.env.json("wait", "--timeout", "0.5")["messages"], [])

    def test_dispatch_identity_and_round_history_survive_a_status_restart(self):
        dispatch = self.env.dispatch("codex", extra_args=["--scope", "scope-a", "--role", "reviewer"])
        first = self.env.json("status", "--dispatch", dispatch)["dispatches"][0]
        self.assertEqual((first["scope"], first["role"], first["round"]), ("scope-a", "reviewer", 1))
        self.assertTrue(first["attempt"].startswith("a-"))
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        task2 = self.env.task.with_name("r2.md")
        task2.write_text("# r2\n")
        report2 = self.env.report.with_name("r2.md")
        self.env.json("continue", dispatch, "--task", str(task2), "--report", str(report2))
        current = self.env.json("status", "--dispatch", dispatch)["dispatches"][0]
        self.assertEqual(current["round"], 2)
        self.assertEqual([r["round"] for r in current["rounds"]], [1, 2])
        self.assertEqual(current["rounds"][0]["state"], "completed")
        self.assertEqual(current["rounds"][0]["report"], str(self.env.report))

    def test_resume_retry_keeps_round_and_closes_old_attempt(self):
        dispatch = self.env.dispatch("claude")
        self.env.end_session(dispatch)
        self.assertEqual(self.env.json("resume", dispatch)["round"], 1)
        rounds = self.env.json("status", "--dispatch", dispatch)["dispatches"][0]["rounds"]
        self.assertEqual([r["round"] for r in rounds], [1, 1])
        self.assertEqual([r["state"] for r in rounds], ["unknown", "running"])
        stale = self.env.run("ask", "stale worker", dispatch=dispatch)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("missing", stale.stderr)

    def test_done_from_old_attempt_is_rejected_after_retry(self):
        dispatch = self.env.dispatch("claude")
        first = self.env.json("status", "--dispatch", dispatch)["dispatches"][0]
        self.env.end_session(dispatch)
        self.env.json("resume", dispatch)
        self.env.report.write_text("# report\n")
        stale = self.env.run("done", "--report", str(self.env.report), "--attempt", first["attempt"],
                             dispatch=dispatch)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("no longer accepted", stale.stderr)

    def test_missing_attempt_question_is_rejected_after_continue_round(self):
        dispatch = self.env.dispatch()
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        task2 = self.env.task.with_name("r2.md")
        task2.write_text("# r2\n")
        self.env.json("continue", dispatch, "--task", str(task2),
                      "--report", str(self.env.report.with_name("r2.md")))
        stale = self.env.run("ask", "old worker", dispatch=dispatch)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("missing", stale.stderr)

    def test_a_resume_decided_on_a_stale_view_does_not_relaunch_a_finished_task(self):
        import concurrent.futures
        dispatch = self.env.dispatch("codex")
        Resume.codex_rollout(type("R", (), {"env": self.env})(), dispatch, "main-session")
        self.env.end_session(dispatch)
        root = self.env.bin.parent / "blocked-home" / "sessions"
        root.mkdir(parents=True)
        fifo = root / "rollout-blocked.jsonl"
        os.mkfifo(fifo)
        text = f"You are a worker dispatched by the controller (dispatch {dispatch}). Read ..."
        payload = (json.dumps({"type": "session_meta", "payload": {"id": "main-session", "source": "cli"}}) + "\n"
                   + json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": text}]}}) + "\n")
        fd = None
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            # This resume blocks while it looks up the conversation, after deciding what to do.
            delayed = pool.submit(lambda: self.env.run("resume", dispatch, env={"CODEX_HOME": str(root.parent)},
                                                       timeout=20))
            for _ in range(500):
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    break
                except OSError:
                    time.sleep(0.01)
            self.env.json("resume", dispatch)
            self.env.report.write_text("# done\n")
            self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
            self.env.end_session(dispatch)
            os.write(fd, payload.encode())
            os.close(fd)
            late = delayed.result()

        self.assertNotEqual(late.returncode, 0)
        self.assertEqual(len([c for c in self.env.host_calls() if c[:2] == ["tmux", "new-session"]]), 2)

    def unconfirmed_new_session(self):
        fake = self.env.bin / "tmux"
        fake.write_text(fake.read_text() + (
            "if sys.argv[1] == 'new-session' and os.environ.get('FAKE_LAUNCH_KILL'):\n"
            "    import signal; os.kill(os.getpid(), signal.SIGTERM)\n"))

    def test_a_dispatch_whose_launch_was_not_confirmed_keeps_its_record(self):
        self.unconfirmed_new_session()

        result = self.env.run("dispatch", "--host", "codex", "--task", str(self.env.task), "--cwd",
                              str(self.env.cwd), "--report", str(self.env.report), env={"FAKE_LAUNCH_KILL": "1"})

        self.assertNotEqual(result.returncode, 0)
        [dispatch] = [line.split()[-1].strip(".;") for line in result.stderr.split() if line.startswith("d-")] or [None]
        self.assertIsNotNone(dispatch, result.stderr)
        self.env.report.write_text("# r1\n")
        done = self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_resume_whose_launch_was_not_confirmed_keeps_the_new_round(self):
        dispatch = self.env.dispatch("claude")
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        self.env.end_session(dispatch)
        self.unconfirmed_new_session()
        task2 = self.env.task.with_name("r2.md")
        task2.write_text("# r2\n")
        report2 = self.env.report.with_name("r2.md")

        result = self.env.run("resume", dispatch, "--task", str(task2), "--report", str(report2),
                              env={"FAKE_LAUNCH_KILL": "1"})
        report2.write_text("# r2\n")
        done = self.env.run("done", "--report", str(report2), dispatch=dispatch)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(done.returncode, 0, done.stderr)



class ReportReuse(unittest.TestCase):
    """W15 (round 7): every round reports to a path the dispatch has never used."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.dispatch = self.env.dispatch("claude")
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)
        self.task2 = self.env.task.with_name("r2.md")
        self.task2.write_text("# r2\n")

    def test_continue_refuses_the_report_path_of_the_round_just_done(self):
        result = self.env.run("continue", self.dispatch, "--task", str(self.task2), "--report", str(self.env.report))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already used", result.stderr)

    def test_continue_refuses_a_report_path_from_any_earlier_round(self):
        report2 = self.env.report.with_name("r2.md")
        self.env.json("continue", self.dispatch, "--task", str(self.task2), "--report", str(report2))
        report2.write_text("# r2\n")
        self.env.run("done", "--report", str(report2), dispatch=self.dispatch)

        result = self.env.run("continue", self.dispatch, "--task", str(self.task2), "--report", str(self.env.report))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already used", result.stderr)

    def test_resume_with_a_task_refuses_a_used_report_path(self):
        self.env.end_session(self.dispatch)

        result = self.env.run("resume", self.dispatch, "--task", str(self.task2), "--report", str(self.env.report))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already used", result.stderr)



class OpenCodeRelay(unittest.TestCase):
    """relay pushes controller messages into an OpenCode controller session over its HTTP API
    (prompt_async), the OpenCode counterpart of `codex queue`."""

    SESSION = "ses_controller1"

    def setUp(self):
        import http.server
        import threading
        self.env = Env()
        self.addCleanup(self.env.close)
        self.dispatch = self.env.dispatch()
        self.env.report.write_text("# report\n")
        self.posts, self.status = [], 204
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                test.posts.append((self.path, json.loads(body)))
                self.send_response(test.status)
                if getattr(test, "location", None):
                    self.send_header("Location", test.location)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)  # cleanups run last-in first-out: shutdown, then close
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def relay(self, url=None, seconds="1.5"):
        return self.env.run("relay", "--opencode-url", url or self.url, "--opencode-session", self.SESSION,
                            "--for", seconds)

    def test_messages_are_posted_into_the_opencode_controller_once(self):
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)

        first = self.relay()
        second = self.relay(seconds="0.8")

        self.assertEqual((first.returncode, second.returncode), (0, 0), first.stderr)
        [(path, body)] = self.posts
        self.assertEqual(path, f"/session/{self.SESSION}/prompt_async")
        [part] = body["parts"]
        self.assertEqual(part["type"], "text")
        self.assertTrue(part["text"].startswith("[router] Messages for the controller: "))
        self.assertIn('"type": "done"', part["text"])

    def test_messages_stay_waiting_when_opencode_is_not_listening(self):
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)
        closed = socket_free_port()

        self.relay(url=f"http://127.0.0.1:{closed}", seconds="1")

        [message] = self.env.json("wait", "--timeout", "1")["messages"]
        self.assertEqual(message["type"], "done")

    def test_an_error_answer_is_logged_as_uncertain_and_not_retried(self):
        self.status = 500
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)

        result = self.relay(seconds="2")

        self.assertEqual(len(self.posts), 1)
        self.assertIn("uncertain", result.stdout)
        self.assertEqual(self.env.json("wait", "--timeout", "0.5")["messages"], [])

    def test_a_redirect_is_not_followed_and_counts_as_uncertain(self):
        # The first endpoint may already have taken the POST; following a redirect could resend it
        # or mistake an unrelated answer for delivery.
        closed = socket_free_port()
        self.status = 302
        self.location = f"http://127.0.0.1:{closed}/elsewhere"
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)

        result = self.relay(seconds="2")

        self.assertEqual(len(self.posts), 1)
        self.assertIn("uncertain", result.stdout)
        self.assertEqual(self.env.json("wait", "--timeout", "0.5")["messages"], [])

    def test_relay_needs_exactly_one_controller(self):
        both = self.env.run("relay", "--codex-thread", CodexRelay.THREAD, "--opencode-url", self.url, "--opencode-session",
                            self.SESSION, "--for", "0.1")
        current = self.env.run("relay", "--codex", "--opencode-url", self.url, "--opencode-session",
                               self.SESSION, "--for", "0.1", env={"CODEX_THREAD_ID": CodexRelay.THREAD})
        neither = self.env.run("relay", "--for", "0.1")
        half = self.env.run("relay", "--opencode-url", self.url, "--for", "0.1")

        self.assertNotEqual(both.returncode, 0)
        self.assertNotEqual(current.returncode, 0)
        self.assertNotEqual(neither.returncode, 0)
        self.assertNotEqual(half.returncode, 0)


def utc_now():
    now = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{int(now % 1 * 1000):03d}Z"


class HostErrors(unittest.TestCase):
    """A worker whose turn ended on a host error (quota, overload) is reported `blocked` with the
    host's own error, read from its conversation record, without waiting for a stall."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.screen.write_text("■ You've hit your usage limit.\n")
        self.still = {"FAKE_SCREEN": str(self.env.screen)}

    def wait(self, timeout="4", stall_after="60"):
        return self.env.json("wait", "--timeout", timeout, "--stall-after", stall_after, env=self.still)

    def codex_rollout(self, dispatch, events):
        day = self.env.bin.parent / "codex-home" / "sessions" / "2026" / "11" / "08"
        day.mkdir(parents=True, exist_ok=True)
        meta = {"type": "session_meta", "payload": {"id": "s-1", "source": "cli"}}
        prompt = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": f"You are a worker dispatched by the controller (dispatch {dispatch}). Read"}]}}
        lines = [meta, prompt] + [{"timestamp": utc_now(), "type": "event_msg", "payload": e} for e in events]
        (day / "rollout-2026-11-08T00-00-00-s-1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    def codex_quota(self, turn="t-1"):
        return {"type": "task_complete", "turn_id": turn, "last_agent_message": None,
                "error": {"message": "You've hit your usage limit. Try again at 3:00 PM.",
                          "codex_error_info": "usage_limit_exceeded"}}

    def test_codex_quota_error_is_reported_once_with_the_raw_message(self):
        dispatch = self.env.dispatch("codex")
        self.codex_rollout(dispatch, [{"type": "task_started", "turn_id": "t-1"}, self.codex_quota()])

        [message] = self.wait()["messages"]
        self.assertEqual((message["type"], message["dispatch"], message["host"], message["kind"]),
                         ("blocked", dispatch, "codex", "usage_limit_exceeded"))
        self.assertIn("Try again at 3:00 PM", message["message"])
        # Neither reported again nor followed by a stall on the same screen.
        self.assertTrue(self.wait(timeout="2.5", stall_after="0.3")["timedOut"])

    def test_claude_rate_limit_is_reported_as_blocked(self):
        dispatch = self.env.dispatch("claude")
        [launch] = [c for c in self.env.host_calls() if c[:2] == ["tmux", "new-session"]]
        session_id = launch[launch.index("--session-id") + 1]
        project = self.env.bin.parent / "claude-home" / "projects" / "-tmp-worktree"
        project.mkdir(parents=True)
        entries = [{"type": "user", "uuid": "u1", "timestamp": utc_now(), "message": {"content": "brief"}},
                   {"type": "assistant", "uuid": "a1", "timestamp": utc_now(), "error": "rate_limit",
                    "isApiErrorMessage": True,
                    "message": {"content": [{"type": "text", "text": "You've hit your 5-hour limit · resets 3am"}]}}]
        (project / f"{session_id}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))

        [message] = self.wait()["messages"]
        self.assertEqual((message["type"], message["kind"], message["message"]),
                         ("blocked", "rate_limit", "You've hit your 5-hour limit · resets 3am"))

    def test_opencode_error_is_reported_as_blocked(self):
        dispatch = self.env.dispatch("opencode")
        db = sqlite3.connect(self.env.bin.parent / "opencode.db")
        db.executescript("CREATE TABLE session (id TEXT, parent_id TEXT, time_created INTEGER);"
                         "CREATE TABLE part (id TEXT, session_id TEXT, data TEXT);"
                         "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);")
        brief = f"You are a worker dispatched by the controller (dispatch {dispatch}). Read"
        db.execute("INSERT INTO session VALUES ('ses_w', NULL, 1)")
        db.execute("INSERT INTO part VALUES ('p1', 'ses_w', ?)", (json.dumps({"type": "text", "text": brief}),))
        now = int(time.time() * 1000)
        db.execute("INSERT INTO message VALUES ('msg_1', 'ses_w', ?, ?)", (now, json.dumps({"role": "user"})))
        db.execute("INSERT INTO message VALUES ('msg_2', 'ses_w', ?, ?)", (now + 1, json.dumps(
            {"role": "assistant", "error": {"name": "APIError", "data": {"message": "Insufficient balance"}}})))
        db.commit()
        db.close()

        [message] = self.wait()["messages"]
        self.assertEqual((message["type"], message["kind"], message["message"]),
                         ("blocked", "APIError", "Insufficient balance"))

    def test_a_turn_that_started_again_after_the_error_is_not_reported(self):
        dispatch = self.env.dispatch("codex")
        self.codex_rollout(dispatch, [self.codex_quota(), {"type": "task_started", "turn_id": "t-2"}])

        self.assertTrue(self.wait(timeout="2.5")["timedOut"])

    def test_an_error_from_before_the_latest_nudge_is_not_reported(self):
        dispatch = self.env.dispatch("codex")
        self.codex_rollout(dispatch, [self.codex_quota()])
        time.sleep(1.1)  # the nudge comes after the error
        self.env.json("nudge", dispatch, "--text", "continue")

        self.assertTrue(self.wait(timeout="2.5")["timedOut"])


    def test_a_replaced_or_truncated_record_is_read_again_from_its_start(self):
        dispatch = self.env.dispatch("codex")
        self.codex_rollout(dispatch, [{"type": "task_started", "turn_id": "t-1"}, self.codex_quota("t-1"),
                                      {"type": "token_count", "info": {"padding": "x" * 4000}}])
        self.assertEqual([m["type"] for m in self.relay_wait()], ["blocked"])
        time.sleep(1.1)
        self.env.json("nudge", dispatch, "--text", "continue")
        self.codex_rollout(dispatch, [self.codex_quota("t-2")])  # shorter than what was read

        [again] = self.relay_wait()
        self.assertEqual((again["type"], again["kind"]), ("blocked", "usage_limit_exceeded"))

    def relay_wait(self):
        """Messages from one long-running relay pass, so the same TurnWatch sees the file change."""
        if not hasattr(self, "relay"):
            self.env.codex_controller(CodexRelay.THREAD)
            env = {k: v for k, v in os.environ.items() if k != "CODEX_THREAD_ID"}
            self.relay = subprocess.Popen(
                [sys.executable, str(ROUTER), "relay", "--codex-thread", CodexRelay.THREAD, "--stall-after", "60"],
                env={**env, "WFC_STATE": str(self.env.state), "CODEX_HOME": str(self.env.bin.parent / "codex-home"),
                     "WFC_CLAUDE_CONFIG": str(self.env.bin.parent / "none.json"),
                     "PATH": f"{self.env.bin}{os.pathsep}{os.environ['PATH']}", **self.still},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(self.relay.wait)
            self.addCleanup(self.relay.kill)
            self.seen = 0
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            queued = [c for c in self.env.host_calls() if c[:2] == ["codex", "queue"]]
            if len(queued) > self.seen:
                batch = json.loads(queued[-1][queued[-1].index("--message") + 1].split(": ", 1)[1])["messages"]
                self.seen = len(queued)
                return batch
            time.sleep(0.2)
        return []

    def test_a_rollout_that_vanishes_during_the_lookup_does_not_stop_the_wait(self):
        dispatch = self.env.dispatch("codex")
        self.codex_rollout(dispatch, [self.codex_quota()])
        day = self.env.bin.parent / "codex-home" / "sessions" / "2026" / "11" / "08"
        (day / "rollout-2026-11-07T00-00-00-gone.jsonl").symlink_to(day / "removed.jsonl")  # listed, then stat fails

        [message] = self.wait()["messages"]
        self.assertEqual((message["type"], message["kind"]), ("blocked", "usage_limit_exceeded"))

    def test_a_malformed_opencode_record_does_not_stop_the_wait(self):
        dispatch = self.env.dispatch("opencode")
        db = sqlite3.connect(self.env.bin.parent / "opencode.db")
        db.executescript("CREATE TABLE session (id TEXT, parent_id TEXT, time_created INTEGER);"
                         "CREATE TABLE part (id TEXT, session_id TEXT, data TEXT);"
                         "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);")
        brief = f"You are a worker dispatched by the controller (dispatch {dispatch}). Read"
        db.execute("INSERT INTO session VALUES ('ses_w', NULL, 1)")
        db.execute("INSERT INTO part VALUES ('p1', 'ses_w', ?)", (json.dumps({"type": "text", "text": brief}),))
        db.execute("INSERT INTO message VALUES ('msg_1', 'ses_w', ?, ?)", (int(time.time() * 1000), "{not json"))
        db.commit()
        db.close()

        # The screen watchdog still reports the stall.
        [message] = self.wait(stall_after="1")["messages"]
        self.assertEqual(message["type"], "stalled")


class Nudge(unittest.TestCase):
    """nudge types a line into a worker still on its task, and records it."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.dispatch = self.env.dispatch()

    def test_the_line_is_typed_into_the_worker_and_recorded(self):
        result = self.env.json("nudge", self.dispatch, "--text", "Quota is back.\nContinue the task.")

        typed = [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]
        self.assertEqual(typed[-1][-1], "[router] Message from the controller: Quota is back. Continue the task.")
        status = self.env.json("status", "--dispatch", self.dispatch)
        self.assertEqual([m["type"] for m in status["dispatches"][0]["messages"]], ["nudge"])
        self.assertEqual(result["nudged"], self.dispatch)

    def test_a_nudge_waits_for_input_already_being_typed_into_the_worker(self):
        import fcntl
        lock = os.open(self.env.state / f"input-{self.dispatch}.lock", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)  # as a continue or reply typing into this session would
        env = {**os.environ, "WFC_STATE": str(self.env.state), "PATH": f"{self.env.bin}{os.pathsep}{os.environ['PATH']}"}
        nudging = subprocess.Popen([sys.executable, str(ROUTER), "nudge", self.dispatch, "--text", "hi"],
                                   stdout=subprocess.DEVNULL, env=env)
        self.addCleanup(nudging.wait)
        self.addCleanup(nudging.kill)
        time.sleep(1)
        typed = lambda: [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]
        self.assertEqual(typed(), [])
        self.assertIsNone(nudging.poll())

        fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(nudging.wait(timeout=10), 0)
        self.assertEqual(typed()[-1][-1], "[router] Message from the controller: hi")

    def test_a_finished_worker_is_given_its_next_task_with_continue_instead(self):
        self.env.report.write_text("# report\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=self.dispatch)

        result = self.env.run("nudge", self.dispatch, "--text", "hi")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("continue", result.stderr)

    def test_a_worker_whose_session_ended_is_resumed_instead(self):
        self.env.end_session(self.dispatch)

        result = self.env.run("nudge", self.dispatch, "--text", "hi")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("resume", result.stderr)


class ResumeInputLock(unittest.TestCase):
    def test_a_resume_waits_for_input_being_typed_before_it_launches(self):
        import fcntl
        env = Env()
        self.addCleanup(env.close)
        dispatch = env.dispatch("claude")
        env.end_session(dispatch)
        lock = os.open(env.state / f"input-{dispatch}.lock", os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        resuming = subprocess.Popen([sys.executable, str(ROUTER), "resume", dispatch], stdout=subprocess.DEVNULL,
                                    env={**os.environ, "WFC_STATE": str(env.state),
                                         "WFC_CLAUDE_CONFIG": str(env.bin.parent / "none.json"),
                                         "PATH": f"{env.bin}{os.pathsep}{os.environ['PATH']}"})
        self.addCleanup(resuming.wait)
        self.addCleanup(resuming.kill)
        launches = lambda: [c for c in env.host_calls() if c[:2] == ["tmux", "new-session"]]
        time.sleep(1)
        self.assertEqual(len(launches()), 1)  # only the dispatch

        fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(resuming.wait(timeout=10), 0)
        self.assertEqual(len(launches()), 2)


class Status(unittest.TestCase):
    """status shows every dispatch and the undelivered messages without taking them."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_dispatch_states_and_undelivered_messages_are_shown_without_delivering_them(self):
        working = self.env.dispatch("codex", extra_args=("--model", "gpt-x"))
        finished = self.env.dispatch("claude")
        self.env.report.write_text("# report\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=finished)

        status = self.env.json("status")

        states = {d["dispatch"]: (d["host"], d["state"], d["tmuxAlive"]) for d in status["dispatches"]}
        self.assertEqual(states, {working: ("codex", "working", True), finished: ("claude", "done", True)})
        self.assertEqual([(m["type"], m["dispatch"]) for m in status["undelivered"]], [("done", finished)])
        [done] = self.env.json("wait", "--timeout", "2")["messages"]
        self.assertEqual(done["type"], "done")  # status did not take it
        self.assertEqual(self.env.json("status")["undelivered"], [])

    def test_active_lists_only_unfinished_dispatches_and_the_done_ones_whose_session_is_alive(self):
        working = self.env.dispatch()
        asking = self.env.dispatch()
        self.env.run("ask", "Red or blue?", dispatch=asking)
        exited = self.env.dispatch()
        self.env.end_session(exited)
        self.env.json("wait", "--timeout", "2")  # the watchdog notices the ended session
        lingering = self.env.dispatch(extra_args=("--label", "p | 11 | impl"))
        closed = self.env.dispatch()
        for finished in (lingering, closed):
            self.env.report.write_text("# report\n")
            self.env.run("done", "--report", str(self.env.report), dispatch=finished)
        self.env.end_session(closed)
        everything = self.env.json("status")

        status = self.env.json("status", "--active")

        self.assertEqual({d["dispatch"]: d["state"] for d in status["dispatches"]},
                         {working: "working", asking: "asking", exited: "exited"})
        self.assertEqual(status["reclaimable"],
                         [{"dispatch": lingering, "label": "p | 11 | impl", "tmux": f"wfc-{lingering}"}])
        # undelivered and waiters are the same as without --active
        self.assertEqual(status["undelivered"], everything["undelivered"])
        self.assertEqual(status["waiters"], everything["waiters"])
        self.assertEqual(len(everything["dispatches"]), 5)
        self.assertNotIn("reclaimable", everything)

    def test_active_without_a_state_database_is_empty(self):
        status = self.env.json("status", "--active")

        self.assertEqual((status["dispatches"], status["reclaimable"], status["undelivered"], status["waiters"]),
                         ([], [], [], []))
        self.assertFalse(self.env.state.exists())

    def test_active_with_dispatch_is_refused(self):
        dispatch = self.env.dispatch()

        result = self.env.run("status", "--active", "--dispatch", dispatch)

        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with", result.stderr)

    def test_a_running_wait_is_listed(self):
        self.env.dispatch()
        env = {**os.environ, "WFC_STATE": str(self.env.state), "PATH": f"{self.env.bin}{os.pathsep}{os.environ['PATH']}"}
        waiting = subprocess.Popen([sys.executable, str(ROUTER), "wait", "--timeout", "30"],
                                   stdout=subprocess.DEVNULL, env=env)
        self.addCleanup(waiting.kill)
        time.sleep(1)

        waiters = self.env.json("status")["waiters"]
        self.assertEqual([(w["pid"], w["kind"]) for w in waiters], [(waiting.pid, "wait")])
        waiting.kill()
        waiting.wait()
        self.assertEqual(self.env.json("status")["waiters"], [])

    def test_an_exited_worker_is_shown_as_exited(self):
        dispatch = self.env.dispatch()
        self.env.end_session(dispatch)
        self.env.json("wait", "--timeout", "2")

        [view] = self.env.json("status")["dispatches"]
        self.assertEqual((view["state"], view["tmuxAlive"], view["last"]["type"]), ("exited", False, "exited"))

    def test_status_without_a_state_database_is_empty_and_creates_nothing(self):
        status = self.env.json("status")

        self.assertEqual((status["dispatches"], status["undelivered"], status["waiters"]), ([], [], []))
        self.assertFalse(self.env.state.exists())

    def test_status_reads_an_old_state_database_without_upgrading_it(self):
        self.env.state.mkdir()
        path = self.env.state / "router.db"
        db = sqlite3.connect(path)
        db.executescript(
            "CREATE TABLE dispatch(id TEXT PRIMARY KEY, host TEXT, cwd TEXT, task TEXT, created REAL);"
            "CREATE TABLE message(id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch TEXT, recipient TEXT,"
            " type TEXT, body TEXT, created REAL, delivered INTEGER DEFAULT 0);"
            "INSERT INTO dispatch VALUES ('old1', 'codex', '/w', '/t.md', 1);"
            "INSERT INTO message (dispatch, recipient, type, body, created) VALUES"
            " ('old1', 'controller', 'done', '{\"report\": \"/r.md\"}', 2);")
        db.close()
        before = path.read_bytes()

        status = self.env.json("status")

        self.assertEqual([(m["type"], m["dispatch"], m["report"]) for m in status["undelivered"]],
                         [("done", "old1", "/r.md")])
        self.assertIn("upgrade", status["schema"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.env.state.iterdir()), ["router.db"])

    def test_a_write_command_adds_the_process_start_to_an_older_waiter_table(self):
        self.env.state.mkdir()
        db = sqlite3.connect(self.env.state / "router.db")
        db.execute("CREATE TABLE waiter (pid INTEGER PRIMARY KEY, kind TEXT, since REAL)")
        db.close()

        self.env.json("wait", "--timeout", "0")

        db = sqlite3.connect(self.env.state / "router.db")
        self.assertIn("process_start", [c[1] for c in db.execute("PRAGMA table_info(waiter)")])
        db.close()

    def test_a_waiter_row_whose_pid_now_belongs_to_another_process_is_not_listed(self):
        self.env.dispatch()
        db = sqlite3.connect(self.env.state / "router.db")
        # This test's own pid is alive, but it is not the process that registered the row.
        db.execute("INSERT INTO waiter (pid, kind, since, process_start) VALUES (?, 'wait', ?, 'an earlier process')",
                   (os.getpid(), time.time()))
        db.commit()
        db.close()

        self.assertEqual(self.env.json("status")["waiters"], [])


# Codex 0.167.0 screens captured on 2026-11-09 (tmux sessions wfc-d-3c8ab0af and wfc-d-ece89726).
READ_ONLY_CHAT = """\
• 第 13 轮审查完成，报告已提交（message 27）。

  Worked for 1m 19s • 6:43 AM


  🔒  Continue in a new chat
      This chat uses OpenAI, but Codex now uses Codex API Service, so this
      chat is read-only.

  c continue in new chat · n start fresh · esc other chats
"""
UNAVAILABLE_CHAT = """\
  Worked for 2m 41s • Nov 6 at 6:36 AM

• This conversation is unavailable. Its cached transcript and draft remain here;
input is paused. Open the agent picker or return to the parent to continue.


› Ask Codex to do anything

  GPT-6.1-Sol low · /path/to/workspace/.worktrees/example-project-5-review · Review …
"""
NO_OPERATION = "■ This conversation is read-only or unavailable; no operation was sent.\n"
PROMPT = "• Done.\n\n\n› Ask Codex to do anything\n\n  GPT-6.1-Sol low · /tmp/worktree\n"


class ReadOnlySessions(unittest.TestCase):
    """A host conversation that has become read-only (Codex after a provider switch) drops what is
    typed into it: the router checks the screen around its typing and after a resume, and the
    watchdog reports such a screen as `blocked` (#13)."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.answer = self.env.bin.parent / "answer.txt"
        self.screens = {"FAKE_SCREEN": str(self.env.screen), "FAKE_ENTER_SCREEN": str(self.answer)}

    def rejecting(self, before=PROMPT):
        """The screen shows `before`; an Enter adds Codex's rejection line below it."""
        self.env.screen.write_text(before)
        self.answer.write_text(before + "\n" + NO_OPERATION + "\n")
        return self.screens

    def sends(self):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]

    def messages(self, dispatch):
        return [m["type"] for m in self.env.json("status", "--dispatch", dispatch)["dispatches"][0]["messages"]]

    def finished(self, dispatch):
        self.env.report.write_text("# r1\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)
        self.env.json("wait", "--timeout", "1")
        task = self.env.task.with_name("113-impl-r2.md")
        task.write_text("# r2\n")
        return task, self.env.report.with_name("r2.md")

    def test_a_nudge_the_session_refused_is_reported_and_not_recorded(self):
        dispatch = self.env.dispatch()

        result = self.env.run("nudge", dispatch, "--text", "continue", env=self.rejecting())

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("This conversation is read-only or unavailable; no operation was sent.", result.stderr)
        self.assertIn("dispatch a new worker", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.messages(dispatch), [])

    def test_a_refused_continue_restores_the_round_so_it_can_be_given_again(self):
        dispatch = self.env.dispatch()
        task, report = self.finished(dispatch)

        first = self.env.run("continue", dispatch, "--task", str(task), "--report", str(report), env=self.rejecting())
        [row] = self.env.json("status", "--dispatch", dispatch)["dispatches"]
        second = self.env.run("continue", dispatch, "--task", str(task), "--report", str(report),
                              env={"FAKE_SCREEN": str(self.env.screen)})

        self.assertNotEqual(first.returncode, 0)
        self.assertIn("no operation was sent", first.stderr)
        self.assertEqual(row["report"], str(self.env.report))
        self.assertEqual([m["type"] for m in row["messages"]], ["done"])
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_a_refused_continue_keeps_a_round_that_moved_on_meanwhile(self):
        dispatch = self.env.dispatch()
        task, report = self.finished(dispatch)
        moved = (f"INSERT INTO message (dispatch, recipient, type, body, created) "
                 f"VALUES ('{dispatch}', 'worker', 'resumed', '{{}}', 0);")

        result = self.env.run("continue", dispatch, "--task", str(task), "--report", str(report),
                              env={**self.rejecting(), "FAKE_ENTER_SQL": moved})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("moved on meanwhile", result.stderr)
        [row] = self.env.json("status", "--dispatch", dispatch)["dispatches"]
        self.assertEqual(row["report"], str(report))
        self.assertEqual([m["type"] for m in row["messages"]], ["done", "continue", "resumed"])

    def test_a_refused_reply_releases_the_question(self):
        dispatch = self.env.dispatch()
        question = self.env.json("ask", "Red or blue?", dispatch=dispatch)["message"]

        first = self.env.run("reply", str(question), "blue", env=self.rejecting())
        second = self.env.run("reply", str(question), "blue", env={"FAKE_SCREEN": str(self.env.screen)})

        self.assertNotEqual(first.returncode, 0)
        self.assertIn("no operation was sent", first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_an_earlier_refusal_still_on_screen_does_not_fail_a_send_that_went_through(self):
        dispatch = self.env.dispatch()
        self.env.screen.write_text(NO_OPERATION + PROMPT)

        result = self.env.run("nudge", dispatch, "--text", "continue", env={"FAKE_SCREEN": str(self.env.screen)})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.messages(dispatch), ["nudge"])

    def test_a_state_notice_drawn_after_the_enter_fails_the_send_even_if_an_old_refusal_scrolled_away(self):
        # Review R2-1: one notice before (an old refusal), one after (a new state notice).
        dispatch = self.env.dispatch()
        self.env.screen.write_text(NO_OPERATION + PROMPT)
        self.answer.write_text(UNAVAILABLE_CHAT)

        result = self.env.run("nudge", dispatch, "--text", "continue", env=self.screens)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("This conversation is unavailable. Its cached transcript and draft remain here; input is paused.",
                      result.stderr)
        self.assertEqual(self.messages(dispatch), [])

    def test_after_a_send_that_went_through_an_old_refusal_on_screen_is_not_reported(self):
        # Review R2-2: the refusal answered an earlier Enter, not the current state of the session.
        dispatch = self.env.dispatch()
        self.env.screen.write_text(NO_OPERATION + PROMPT)
        env = {"FAKE_SCREEN": str(self.env.screen)}

        self.env.json("nudge", dispatch, "--text", "continue", env=env)
        after = self.env.json("wait", "--timeout", "1", "--stall-after", "60", env=env)

        self.assertEqual(after["messages"], [])

    def test_a_screen_with_only_old_refusals_stalls_as_usual(self):
        dispatch = self.env.dispatch()
        self.env.screen.write_text(NO_OPERATION + NO_OPERATION + PROMPT)

        [message] = self.env.json("wait", "--timeout", "2", "--stall-after", "0.3",
                                  env={"FAKE_SCREEN": str(self.env.screen)})["messages"]

        self.assertEqual((message["type"], message["dispatch"]), ("stalled", dispatch))

    def test_a_normal_send_is_followed_by_no_blocked_report(self):
        dispatch = self.env.dispatch()
        self.env.screen.write_text(PROMPT)
        env = {"FAKE_SCREEN": str(self.env.screen)}

        self.env.json("nudge", dispatch, "--text", "continue", env=env)
        after = self.env.json("wait", "--timeout", "1", "--stall-after", "60", env=env)

        self.assertEqual(after["messages"], [])

    def test_nothing_is_typed_into_a_session_whose_screen_says_it_is_read_only(self):
        # The read-only banner takes single keys (c, n) as choices: typing there would pick one.
        dispatch = self.env.dispatch()
        for screen in (READ_ONLY_CHAT, UNAVAILABLE_CHAT):
            with self.subTest(screen=screen[:20]):
                self.env.screen.write_text(screen)

                result = self.env.run("nudge", dispatch, "--text", "continue", env={"FAKE_SCREEN": str(self.env.screen)})

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("dispatch a new worker", result.stderr)
                self.assertEqual(self.sends(), [])
                self.assertEqual(self.messages(dispatch), [])

    def test_the_typing_is_checked_on_the_screen_after_its_enter(self):
        dispatch = self.env.dispatch()

        self.env.json("nudge", dispatch, "--text", "continue")

        calls = [c[:2] for c in self.env.host_calls()]
        enter = max(i for i, c in enumerate(self.env.host_calls()) if c[-1] == "Enter")
        self.assertIn(["tmux", "capture-pane"], calls[enter:])

    def test_a_normal_send_waits_at_most_the_check_window(self):
        dispatch = self.env.dispatch()
        started = time.monotonic()

        self.env.json("nudge", dispatch, "--text", "continue", env={"WFC_TYPED_CHECK_SECONDS": "1.5"})

        self.assertLess(time.monotonic() - started, 0.5 + 1.5 + 1.5)  # Enter delay, window, process start

    def test_the_watchdog_reports_a_read_only_screen_once_as_blocked(self):
        dispatch = self.env.dispatch()
        self.env.screen.write_text(READ_ONLY_CHAT)
        env = {"FAKE_SCREEN": str(self.env.screen)}

        first = self.env.json("wait", "--timeout", "2", "--stall-after", "60", env=env)
        again = self.env.json("wait", "--timeout", "1.5", "--stall-after", "0.3", env=env)

        [message] = first["messages"]
        self.assertEqual((message["type"], message["dispatch"], message["host"], message["kind"], message["message"]),
                         ("blocked", dispatch, "codex", "session_read_only",
                          "This chat uses OpenAI, but Codex now uses Codex API Service, so this chat is read-only."))
        self.assertTrue(again["timedOut"], again)  # neither reported again nor followed by a stall

    def test_the_watchdog_reports_an_unavailable_screen_with_its_line(self):
        self.env.dispatch()
        self.env.screen.write_text(UNAVAILABLE_CHAT + NO_OPERATION)

        [message] = self.env.json("wait", "--timeout", "2", "--stall-after", "60",
                                  env={"FAKE_SCREEN": str(self.env.screen)})["messages"]

        self.assertEqual((message["type"], message["kind"], message["message"]),
                         ("blocked", "session_read_only",
                          "This conversation is unavailable. Its cached transcript and draft remain here; "
                          "input is paused."))

    def test_a_worker_quoting_the_notice_in_its_output_is_not_reported(self):
        self.env.dispatch()
        self.env.screen.write_text("• Ran grep -n read-only router.py\n"
                                   "  └ 12: ■ This conversation is read-only or unavailable; no operation was sent.\n"
                                   "  │ so this chat is read-only.\n" + PROMPT)

        [message] = self.env.json("wait", "--timeout", "2", "--stall-after", "0.3",
                                  env={"FAKE_SCREEN": str(self.env.screen)})["messages"]

        self.assertEqual(message["type"], "stalled")

    def test_a_worker_quoting_the_state_notices_in_its_output_is_not_reported(self):
        self.env.dispatch()
        self.env.screen.write_text("• Ran grep -n -A1 'This' router.py\n"
                                   "  └ This chat uses OpenAI, but Codex now uses Codex API Service, so this chat\n"
                                   "    is read-only.\n"
                                   "  │ This conversation is unavailable. Its cached transcript and draft remain\n"
                                   "    here; input is paused.\n" + PROMPT)

        [message] = self.env.json("wait", "--timeout", "2", "--stall-after", "0.3",
                                  env={"FAKE_SCREEN": str(self.env.screen)})["messages"]

        self.assertEqual(message["type"], "stalled")

    def test_a_codex_resume_that_opens_read_only_is_reported(self):
        dispatch = self.env.dispatch()
        Resume.codex_rollout(type("R", (), {"env": self.env})(), dispatch, "main-2222")
        self.env.end_session(dispatch)
        self.env.screen.write_text(READ_ONLY_CHAT)
        env = {"FAKE_SCREEN": str(self.env.screen)}

        result = self.env.run("resume", dispatch, env=env)
        after = self.env.json("wait", "--timeout", "1.5", "--stall-after", "60", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("so this chat is read-only", result.stderr)
        self.assertIn("dispatch a new worker", result.stderr)
        self.assertTrue(after["timedOut"], after)  # the resume already said so

    def test_an_opencode_resume_whose_brief_was_refused_is_reported(self):
        dispatch = self.env.dispatch("opencode", extra_args=("--model", "m"))
        Resume.opencode_session(type("R", (), {"env": self.env})(), dispatch, "ses_main")
        self.env.end_session(dispatch)

        result = self.env.run("resume", dispatch, env=self.rejecting("> ready\n"), timeout=20)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no operation was sent", result.stderr)
        self.assertIn("dispatch a new worker", result.stderr)


# Codex 0.167.0, example-project 2026-11-10, message 176: the answer to question 174 still in the input
# box at the bottom (the last `›` paragraph above the footer), its Enter dropped (#18).
ENTER_DROPPED_174 = """\
• 已向总控提交 ETF 母 ETF 映射来源裁决请求（消息 174），等待决策后继续。
  Worked for 16m 47s • 3:14 AM
› [router] Answer to question 174: 批准page-scrape，通过现有统一UpstreamFetcher
  只读抓取官方基金页。条件：必须核实页面明确属于请求的基金代码，并定位明确“查看
  相关ETF”语义的链接关系，不是从任意六位href猜；接受目标官方同域https基金详情
  URL且代码不同于联接基金，仅允许固定主机/路径拼接，不跟随任意外部href，避免引
  入任意URL访问。映射代码需通过母ETF基础信息或可信股票/ETF行情响应核实其身份/市
  场，不能仅语法六位就证明是ETF。失败/多义/同页冲突/身份不符unavailable，不能名
  称猜测。样本优先保存真实原始页面或可复核完整相关区块及URL/请求基金/母ETF/采集
  时刻/hash，片段若裁剪必须标明并保留原文到报告证据目录，不能把片段称原始完整响
  应。页面一般没有可靠来源更新时间；采集时间只是fetchedAt，不能当作映射官方发布
  时间或估值quote.time，按明确缓存TTL缓存映射并说明刷新策略。HTTP回归覆盖
  110020→510310真实成功、错误页面基金代码、无/多相关链接、外域/假链接、母ETF非
  ETF/无报价、坏响应及恢复。无需等统一API，继续原#7任务。
  GPT-6.1-Sol medium · /path/to/workspace/.worktrees/…  ⚠ 1 warning · f2 to view
"""
# The same answer once submitted: Codex repeats it in the transcript, also after a `›`, and the
# input box below is empty again (its placeholder shows).
SUBMITTED_174 = ("\n".join(ENTER_DROPPED_174.splitlines()[:-1])
                 + "\n\n• Working (3s • esc to interrupt)\n\n\n› Ask Codex to do anything\n\n"
                 + ENTER_DROPPED_174.splitlines()[-1] + "\n")
ANSWER_174 = "".join(line.strip() for line in ENTER_DROPPED_174.splitlines()[2:14]).split(": ", 1)[1]


def codex_box(*lines):
    """A Codex screen whose input box holds `lines` (the first after `› `, the rest indented)."""
    box = "\n".join(["› " + lines[0], *("  " + line for line in lines[1:])])
    return "• Done.\n\n\n" + box + "\n\n  GPT-6.1-Sol low · /tmp/worktree\n"


NUDGED = "[router] Message from the controller: continue"


class DroppedEnter(unittest.TestCase):
    """A long text can arrive after its Enter, which the TUI then drops: the text stays in Codex's
    input box. The router looks at that box (not the transcript), presses Enter again while the box
    still holds the text, and reports PARTIAL when it never leaves (#18)."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def screen(self, name, text):
        path = self.env.bin.parent / name
        path.write_text(text)
        return str(path)

    def enters(self):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and c[-1] == "Enter"]

    def texts(self):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "send-keys"] and "-l" in c]

    def question_174(self, dispatch):
        db = sqlite3.connect(self.env.state / "router.db")
        db.execute("INSERT INTO message (id, dispatch, recipient, type, body, created) VALUES "
                   "(174, ?, 'controller', 'question', ?, ?)",
                   (dispatch, json.dumps({"question": "Which source?", "options": None}), time.time()))
        db.commit()
        db.close()

    def test_the_sample_answer_left_in_the_box_gets_one_more_enter(self):
        dispatch = self.env.dispatch()
        self.question_174(dispatch)
        env = {"FAKE_SCREEN": self.screen("now.txt", ENTER_DROPPED_174),
               "FAKE_ENTER_SCREENS": os.pathsep.join([self.screen("dropped.txt", ENTER_DROPPED_174),
                                                      self.screen("submitted.txt", SUBMITTED_174)])}

        result = self.env.run("reply", "174", ANSWER_174, env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.texts()), 1)
        self.assertEqual(len(self.enters()), 2)

    def test_an_answer_that_never_leaves_the_box_is_partial_and_not_typed_again(self):
        dispatch = self.env.dispatch()
        self.question_174(dispatch)
        env = {"FAKE_SCREEN": self.screen("now.txt", ENTER_DROPPED_174)}

        first = self.env.run("reply", "174", ANSWER_174, env=env)
        second = self.env.run("reply", "174", ANSWER_174, env=env)

        self.assertNotEqual(first.returncode, 0)
        self.assertIn("its Enter was not confirmed", first.stderr)
        self.assertIn("tmux attach", first.stderr)
        self.assertIn("already answered", second.stderr)  # the claim stays
        self.assertEqual(len(self.texts()), 1)
        self.assertEqual(len(self.enters()), 3)  # the first Enter and at most two more

    def test_the_submitted_answer_in_the_transcript_does_not_count_as_the_box(self):
        dispatch = self.env.dispatch()
        self.question_174(dispatch)
        env = {"FAKE_SCREEN": self.screen("now.txt", ENTER_DROPPED_174),
               "FAKE_ENTER_SCREEN": self.screen("submitted.txt", SUBMITTED_174)}

        result = self.env.run("reply", "174", ANSWER_174, env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.enters()), 1)

    def test_no_enter_into_an_empty_box(self):
        # An Enter into an empty box is never pressed, so a late first Enter cannot be doubled.
        dispatch = self.env.dispatch()
        env = {"FAKE_SCREEN": self.screen("now.txt", PROMPT)}

        result = self.env.run("nudge", dispatch, "--text", "continue", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.enters()), 1)

    def test_text_holding_the_placeholder_is_not_entered_again_once_the_box_is_empty(self):
        # Review R6-1: the empty box shows its placeholder, which the typed text may contain.
        dispatch = self.env.dispatch()
        env = {"FAKE_SCREEN": self.screen("now.txt", PROMPT)}

        result = self.env.run("nudge", dispatch, "--text", "the composer says: Ask Codex to do anything",
                              env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.enters()), 1)

    def test_a_placeholder_not_in_the_table_inside_the_text_is_not_entered_again(self):
        # An unknown placeholder cannot be told from text by itself; a box that is not scrolled must
        # start where the typed text starts, which no placeholder does.
        dispatch = self.env.dispatch()
        env = {"FAKE_SCREEN": self.screen("now.txt", codex_box("Explain this codebase"))}

        result = self.env.run("nudge", dispatch, "--text", "Explain this codebase", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.enters()), 1)

    def test_no_enter_while_the_box_holds_text_the_router_did_not_type(self):
        dispatch = self.env.dispatch()
        env = {"FAKE_SCREEN": self.screen("now.txt", codex_box("fix the flaky test"))}

        result = self.env.run("nudge", dispatch, "--text", "continue", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.enters()), 1)

    def test_no_enter_while_the_box_is_still_changing(self):
        dispatch = self.env.dispatch()
        env = {"FAKE_SCREEN": self.screen("now.txt", codex_box(NUDGED)),
               "FAKE_SCREEN_ALT": self.screen("drawing.txt", codex_box("[router] Message from the"))}

        result = self.env.run("nudge", dispatch, "--text", "continue", env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.enters()), 1)

    def test_a_box_scrolled_past_its_first_line_still_counts(self):
        # Codex scrolls a tall box and marks it with ↑ at the right edge (probe, Codex 0.167.0).
        dispatch = self.env.dispatch()
        long_text = "x" * 40 + " " + "y" * 40
        env = {"FAKE_SCREEN": self.screen("now.txt", codex_box("y" * 40 + "  ↑")),
               "FAKE_ENTER_SCREENS": os.pathsep.join([self.screen("dropped.txt", codex_box("y" * 40 + "  ↑")),
                                                      self.screen("submitted.txt", PROMPT)])}

        result = self.env.run("nudge", dispatch, "--text", long_text, env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.enters()), 2)

    def test_a_refusal_answering_the_extra_enter_is_read_only(self):
        dispatch = self.env.dispatch()
        env = {"FAKE_SCREEN": self.screen("now.txt", codex_box(NUDGED)),
               "FAKE_ENTER_SCREENS": os.pathsep.join([self.screen("dropped.txt", codex_box(NUDGED)),
                                                      self.screen("refused.txt", NO_OPERATION + codex_box(NUDGED))])}

        result = self.env.run("nudge", dispatch, "--text", "continue", env=env)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no operation was sent", result.stderr)
        self.assertEqual(self.env.json("status", "--dispatch", dispatch)["dispatches"][0]["messages"], [])

    def test_hosts_without_a_known_input_box_get_one_enter_as_before(self):
        for host in ("claude", "opencode"):
            with self.subTest(host=host):
                dispatch = self.env.dispatch(host, extra_args=("--model", "m"))
                before = len(self.enters())
                env = {"FAKE_SCREEN": self.screen("now.txt", codex_box(NUDGED))}

                result = self.env.run("nudge", dispatch, "--text", "continue", env=env)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.enters()) - before, 1)


def ps_start(pid):
    """`ps lstart` of `pid` in UTC, as a time; the format waiters were registered with up to 0.4.4."""
    text = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                          env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    return calendar.timegm(time.strptime(text, "%a %b %d %H:%M:%S %Y"))


def lstart_text(epoch):
    t = time.gmtime(epoch)
    return time.strftime("%a %b ", t) + f"{t.tm_mday:2d}" + time.strftime(" %H:%M:%S %Y", t)


class WaiterLiveness(unittest.TestCase):
    """`ps lstart` is the boot time plus the start offset, so it moves when the clock is corrected:
    a waiter is matched on Linux by its /proc starttime, elsewhere by lstart within seconds (#18)."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.dispatch()

    def register(self, process_start):
        db = sqlite3.connect(self.env.state / "router.db")
        db.execute("INSERT INTO waiter (pid, kind, since, process_start) VALUES (?, 'relay', ?, ?)",
                   (os.getpid(), time.time(), process_start))
        db.commit()
        db.close()

    def test_an_older_row_whose_lstart_drifted_a_few_seconds_is_still_listed(self):
        self.register(lstart_text(ps_start(os.getpid()) + 3))

        self.assertEqual([w["pid"] for w in self.env.json("status")["waiters"]], [os.getpid()])

    def test_an_older_row_whose_lstart_is_a_minute_off_is_another_process(self):
        self.register(lstart_text(ps_start(os.getpid()) - 60))

        self.assertEqual(self.env.json("status")["waiters"], [])

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux /proc")
    def test_on_linux_a_waiter_is_registered_and_matched_by_its_proc_starttime(self):
        waiter = subprocess.Popen([sys.executable, str(ROUTER), "wait", "--timeout", "5"],
                                  env=self.env.router_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(waiter.wait)
        self.addCleanup(waiter.kill)
        deadline = time.monotonic() + 5
        row = None
        while row is None and time.monotonic() < deadline:
            db = sqlite3.connect(self.env.state / "router.db")
            row = db.execute("SELECT process_start FROM waiter WHERE pid = ?", (waiter.pid,)).fetchone()
            db.close()
            time.sleep(0.05)
        stat = Path(f"/proc/{waiter.pid}/stat").read_text()
        ticks = stat[stat.rindex(")") + 2:].split()[19]

        self.assertIsNotNone(row)
        self.assertTrue(row[0].endswith(":" + ticks), row[0])
        self.assertEqual([w["pid"] for w in self.env.json("status")["waiters"]], [waiter.pid])

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux /proc")
    def test_on_linux_a_row_with_another_starttime_is_another_process(self):
        stat = Path("/proc/self/stat").read_text()
        ticks = int(stat[stat.rindex(")") + 2:].split()[19])
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.register(f"linux:{boot}:{ticks + 1}")

        self.assertEqual(self.env.json("status")["waiters"], [])


def socket_free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Reclaim(unittest.TestCase):
    """reclaim lists the tmux sessions of finished dispatches and closes them only with --yes;
    an unfinished one only when named. router.db keeps every record, and resume still works."""

    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def finish(self, dispatch):
        self.env.report.write_text("# report\n")
        self.env.run("done", "--report", str(self.env.report), dispatch=dispatch)

    def kills(self):
        return [c for c in self.env.host_calls() if c[:2] == ["tmux", "kill-session"]]

    def alive(self, dispatch):
        return (self.env.sessions / f"wfc-{dispatch}").exists()

    def messages(self, dispatch):
        return self.env.json("status", "--dispatch", dispatch)["dispatches"][0]["messages"]

    def db_rows(self):
        db = sqlite3.connect(self.env.state / "router.db")
        rows = (db.execute("SELECT * FROM dispatch ORDER BY id").fetchall(),
                db.execute("SELECT * FROM message ORDER BY id").fetchall())
        db.close()
        return rows

    def test_default_only_lists_the_done_dispatches_with_a_live_session_and_changes_nothing(self):
        working = self.env.dispatch()
        lingering = self.env.dispatch(extra_args=("--label", "p | 16 | impl"))
        closed = self.env.dispatch()
        for finished in (lingering, closed):
            self.finish(finished)
        self.env.end_session(closed)
        before = self.db_rows()

        listed = self.env.json("reclaim")

        self.assertEqual(listed["sessions"], [{"dispatch": lingering, "label": "p | 16 | impl", "tmux": f"wfc-{lingering}",
                                               "state": "done", "reclaimable": True, "result": "listed"}])
        self.assertFalse(listed["yes"])
        self.assertEqual(self.kills(), [])
        self.assertTrue(self.alive(lingering) and self.alive(working))
        self.assertEqual(self.db_rows(), before)

    def test_default_without_a_state_database_lists_nothing_and_creates_nothing(self):
        self.assertEqual(self.env.json("reclaim")["sessions"], [])
        self.assertFalse(self.env.state.exists())

    def test_yes_closes_the_done_sessions_and_records_reclaimed_for_the_worker(self):
        working = self.env.dispatch()
        lingering = self.env.dispatch()
        self.finish(lingering)
        self.env.json("wait", "--timeout", "2")  # the controller took the done

        result = self.env.json("reclaim", "--yes")

        self.assertEqual([(s["dispatch"], s["result"]) for s in result["sessions"]], [(lingering, "reclaimed")])
        self.assertEqual(self.kills(), [["tmux", "kill-session", "-t", f"wfc-{lingering}"]])
        self.assertFalse(self.alive(lingering))
        self.assertTrue(self.alive(working))
        last = self.messages(lingering)[-1]
        self.assertEqual((last["type"], last["to"]), ("reclaimed", "worker"))
        status = self.env.json("status")
        views = {d["dispatch"]: d for d in status["dispatches"]}
        self.assertEqual(views[lingering]["state"], "done")
        self.assertEqual(views[lingering]["reclaimed"], last["at"])
        self.assertNotIn("reclaimed", views[working])
        self.assertEqual(status["undelivered"], [])  # not for the controller
        self.assertEqual(self.env.json("status", "--active")["reclaimable"], [])
        self.assertEqual(self.env.json("reclaim")["sessions"], [])
        self.assertEqual(len(self.db_rows()[0]), 2)  # the records stay

    def test_an_unfinished_dispatch_is_closed_only_when_named_with_yes(self):
        stale = self.env.dispatch()

        self.assertEqual(self.env.json("reclaim", "--yes")["sessions"], [])
        [named] = self.env.json("reclaim", "--dispatch", stale)["sessions"]
        self.assertEqual((named["dispatch"], named["state"], named["reclaimable"], named["result"]),
                         (stale, "working", False, "listed"))
        self.assertIn("not in reclaimable", named["note"])
        self.assertEqual(self.kills(), [])
        self.assertTrue(self.alive(stale))

        [closed] = self.env.json("reclaim", "--dispatch", stale, "--yes")["sessions"]

        self.assertEqual((closed["result"], closed["reclaimable"]), ("reclaimed", False))
        self.assertIn("not in reclaimable", closed["note"])
        self.assertFalse(self.alive(stale))
        [view] = self.env.json("status", "--active")["dispatches"]
        self.assertEqual((view["dispatch"], view["state"]), (stale, "reclaimed"))
        # the watchdog does not report the closed session as exited
        self.assertTrue(self.env.json("wait", "--timeout", "1", "--stall-after", "0.3")["timedOut"])

    def test_an_unknown_named_dispatch_is_refused_before_anything_is_closed(self):
        lingering = self.env.dispatch()
        self.finish(lingering)

        result = self.env.run("reclaim", "--dispatch", lingering, "--dispatch", "d-nosuch", "--yes")

        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown dispatch d-nosuch", result.stderr)
        self.assertEqual(self.kills(), [])

    def test_a_session_of_another_state_directory_is_refused(self):
        other, unmarked = self.env.dispatch(), self.env.dispatch()
        for finished in (other, unmarked):
            self.finish(finished)
        (self.env.sessions / f"wfc-{other}").write_text(json.dumps({"WFC_STATE": "/elsewhere/state"}))
        (self.env.sessions / f"wfc-{unmarked}").write_text("{}")

        listed = {s["dispatch"]: s for s in self.env.json("reclaim")["sessions"]}
        result = self.env.run("reclaim", "--yes")

        self.assertEqual({d: s["result"] for d, s in listed.items()}, {other: "refused", unmarked: "refused"})
        self.assertIn("/elsewhere/state", listed[other]["error"])
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"wfc-{other}", result.stderr)
        self.assertEqual(self.kills(), [])
        self.assertTrue(self.alive(other) and self.alive(unmarked))
        self.assertNotIn("reclaimed", [m["type"] for m in self.messages(other) + self.messages(unmarked)])

    def test_a_gone_session_is_skipped_and_a_failed_close_does_not_stop_the_rest(self):
        failing, closing, gone = self.env.dispatch(), self.env.dispatch(), self.env.dispatch()
        for finished in (failing, closing, gone):
            self.finish(finished)
        self.env.end_session(gone)

        result = self.env.run("reclaim", "--yes", "--dispatch", failing, "--dispatch", closing, "--dispatch", gone,
                              env={"FAKE_KILL_FAIL": f"wfc-{failing}"})

        self.assertEqual(result.returncode, 1)
        self.assertIn(f"wfc-{failing}", result.stderr)
        sessions = {s["dispatch"]: s for s in json.loads(result.stdout)["sessions"]}
        self.assertEqual({d: s["result"] for d, s in sessions.items()},
                         {failing: "failed", closing: "reclaimed", gone: "gone"})
        self.assertIn("no longer exists", sessions[gone]["note"])
        self.assertTrue(self.alive(failing))
        self.assertFalse(self.alive(closing))
        for kept in (failing, gone):
            self.assertNotIn("reclaimed", [m["type"] for m in self.messages(kept)])

    def continue_while_reclaiming(self, first, second, *named):
        """Start `reclaim --yes`, hold it at `first`'s input lock (after it listed its targets and
        checked `first`'s owner), give `second` its next task with `continue`, then let it go on."""
        lock = os.open(self.env.state / f"input-{first}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        reclaiming = subprocess.Popen([sys.executable, str(ROUTER), "reclaim", "--yes", *named], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, env=self.env.router_env())
        self.addCleanup(reclaiming.kill)
        deadline = time.monotonic() + 10
        while not any(c[:2] == ["tmux", "show-environment"] and f"wfc-{first}" in c for c in self.env.host_calls()):
            self.assertLess(time.monotonic(), deadline, "reclaim never reached the first session")
            time.sleep(0.05)
        time.sleep(0.3)  # past its ownership check, waiting for the lock
        nxt = self.env.report.parent / "113-impl-r2.md"
        self.env.json("continue", second, "--task", str(self.env.task), "--report", str(nxt))
        self.assertEqual(self.env.json("status", "--dispatch", second)["dispatches"][0]["state"], "working")
        fcntl.flock(lock, fcntl.LOCK_UN)
        return reclaiming

    def test_a_default_target_given_a_new_task_after_listing_is_left_running(self):
        first, second = self.env.dispatch(), self.env.dispatch()
        for finished in (first, second):
            self.finish(finished)
        reclaiming = self.continue_while_reclaiming(first, second)

        out, err = reclaiming.communicate(timeout=10)

        sessions = {s["dispatch"]: s for s in json.loads(out)["sessions"]}
        self.assertEqual((sessions[first]["result"], sessions[second]["result"]), ("reclaimed", "changed"), err)
        self.assertEqual(reclaiming.returncode, 0)
        self.assertTrue(self.alive(second))
        self.assertEqual(self.kills(), [["tmux", "kill-session", "-t", f"wfc-{first}"]])
        self.assertNotIn("reclaimed", [m["type"] for m in self.messages(second)])

    def test_a_named_dispatch_is_closed_even_if_it_got_a_new_task_meanwhile(self):
        """Naming a dispatch is the user's explicit decision to close it, whatever its task's state."""
        first, second = self.env.dispatch(), self.env.dispatch()
        for finished in (first, second):
            self.finish(finished)
        reclaiming = self.continue_while_reclaiming(first, second, "--dispatch", first, "--dispatch", second)

        out, err = reclaiming.communicate(timeout=10)

        sessions = {s["dispatch"]: s for s in json.loads(out)["sessions"]}
        self.assertEqual((sessions[first]["result"], sessions[second]["result"]), ("reclaimed", "reclaimed"), err)
        self.assertFalse(self.alive(second))

    def test_resume_after_reclaiming_an_unfinished_dispatch_finishes_its_task_and_is_watched_again(self):
        dispatch = self.env.dispatch("claude")
        self.env.json("reclaim", "--dispatch", dispatch, "--yes")
        self.assertTrue(self.env.json("wait", "--timeout", "1")["timedOut"])

        resumed = self.env.json("resume", dispatch)

        self.assertEqual(resumed["resumed"], dispatch)
        self.assertTrue(self.alive(dispatch))
        self.assertEqual(self.env.json("status", "--dispatch", dispatch)["dispatches"][0]["state"], "working")
        self.assertNotIn("reclaimed", self.env.json("status", "--dispatch", dispatch)["dispatches"][0])
        self.env.end_session(dispatch)
        [exited] = self.env.json("wait", "--timeout", "2")["messages"]
        self.assertEqual((exited["type"], exited["dispatch"]), ("exited", dispatch))

    def test_resume_after_reclaiming_a_done_dispatch_takes_its_next_task(self):
        dispatch = self.env.dispatch("claude")
        self.finish(dispatch)
        self.env.json("reclaim", "--yes")

        unfinished = self.env.run("resume", dispatch)  # the round is still done: reclaim did not move it
        nxt = self.env.report.parent / "113-impl-r2.md"
        resumed = self.env.json("resume", dispatch, "--task", str(self.env.task), "--report", str(nxt))

        self.assertIn("already finished its task", unfinished.stderr)
        self.assertEqual(resumed["resumed"], dispatch)
        self.assertEqual(self.env.json("status", "--dispatch", dispatch)["dispatches"][0]["state"], "working")
        nxt.write_text("# r2\n")
        self.env.run("done", "--report", str(nxt), dispatch=dispatch)
        self.assertEqual([m["type"] for m in self.env.json("wait", "--timeout", "2")["messages"]], ["done", "done"])


if __name__ == "__main__":
    unittest.main()
