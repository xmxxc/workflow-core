"""Tests for notify_controller.py through its command-line interface."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "notify_controller.py"


def run(args, stdin="", env=None, timeout=10):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **(env or {})},
    )


class Workspace:
    """A worker directory with a task binding, a report path and a Claude events dir."""

    def __init__(self, controller=None):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.worker = root / "worktree"
        self.events = root / "events"
        self.report = root / "reports" / "113-impl-r1.md"
        (self.worker / ".workflow-core").mkdir(parents=True)
        self.report.parent.mkdir()
        self.controller = controller or {"host": "claude", "events_dir": str(self.events)}
        (self.worker / ".workflow-core" / "task.json").write_text(json.dumps(
            {"ticket": "113", "report": str(self.report), "controller": self.controller}))

    def stop(self, host="claude", **fields):
        payload = {"hook_event_name": "Stop", "cwd": str(self.worker),
                   "last_assistant_message": "Report written.", **fields}
        return run(["hook", host], json.dumps(payload))

    def events_written(self):
        if not self.events.exists():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.events.glob("*.json"))]

    def close(self):
        self.tmp.cleanup()


class HookClassification(unittest.TestCase):
    def setUp(self):
        self.ws = Workspace()
        self.addCleanup(self.ws.close)

    def test_stop_with_report_wakes_controller_with_done(self):
        self.ws.report.write_text("# report\n")

        result = self.ws.stop()

        self.assertEqual(result.returncode, 0, result.stderr)
        [event] = self.ws.events_written()
        self.assertEqual(event["kind"], "done")
        self.assertEqual(event["ticket"], "113")
        self.assertEqual(event["report"], str(self.ws.report))

    def test_same_report_is_announced_once_and_again_after_it_changes(self):
        self.ws.report.write_text("# report\n")
        self.ws.stop()
        self.ws.stop()
        self.assertEqual(len(self.ws.events_written()), 1)

        self.ws.report.write_text("# report, round 2 with fixes\n")
        self.ws.stop()

        self.assertEqual([e["kind"] for e in self.ws.events_written()], ["done", "done"])

    def test_turn_with_background_tasks_still_running_is_not_announced(self):
        self.ws.report.write_text("# report\n")

        self.ws.stop(background_tasks=[{"id": "b6l3kdx27", "command": "npm test"}])

        self.assertEqual(self.ws.events_written(), [])

    def test_stop_without_report_asks_for_a_person_with_the_last_message(self):
        self.ws.stop(last_assistant_message="Which color do you prefer: red or blue?")

        [event] = self.ws.events_written()
        self.assertEqual(event["kind"], "needs-person")
        self.assertEqual(event["question"], "Which color do you prefer: red or blue?")

    def test_structured_question_asks_for_a_person_even_when_a_report_exists(self):
        self.ws.report.write_text("# report\n")
        claude = {"hook_event_name": "Notification", "cwd": str(self.ws.worker),
                  "notification_type": "permission_prompt", "message": "Claude needs your permission"}
        opencode = {"hook_event_name": "question.asked", "cwd": str(self.ws.worker),
                    "questions": [{"question": "Do you prefer red or blue?", "header": "Color"}]}

        run(["hook", "claude"], json.dumps(claude))
        run(["hook", "opencode"], json.dumps(opencode))

        events = self.ws.events_written()
        self.assertEqual([e["kind"] for e in events], ["needs-person", "needs-person"])
        self.assertEqual([e["question"] for e in events],
                         ["Claude needs your permission", "Do you prefer red or blue?"])


def event_from_message(text):
    """Delivered messages carry the event as JSON after a fixed prefix."""
    prefix = "[workflow-core] "
    assert text.startswith(prefix), text
    return json.loads(text[len(prefix):])


class Delivery(unittest.TestCase):
    def test_codex_controller_is_woken_with_codex_queue_resume(self):
        ws = Workspace({"host": "codex", "thread": "01a1aa97-77da-7fc3-92ad-b9e7a2eb7fe4"})
        self.addCleanup(ws.close)
        ws.report.write_text("# report\n")
        bin_dir = Path(ws.tmp.name) / "bin"
        bin_dir.mkdir()
        argv_log = Path(ws.tmp.name) / "codex-argv.json"
        fake = bin_dir / "codex"
        fake.write_text(f"#!{sys.executable}\nimport json, sys\n"
                        f"open({str(argv_log)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
        fake.chmod(0o755)

        payload = {"hook_event_name": "Stop", "cwd": str(ws.worker), "last_assistant_message": "Done."}
        result = run(["hook", "codex"], json.dumps(payload),
                     env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})

        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(argv_log.read_text())
        self.assertEqual(argv[:3], ["queue", "--thread", "01a1aa97-77da-7fc3-92ad-b9e7a2eb7fe4"])
        self.assertEqual(argv[3], "--message")
        self.assertEqual(argv[5:], ["--resume"])
        event = event_from_message(argv[4])
        self.assertEqual((event["ticket"], event["kind"]), ("113", "done"))


if __name__ == "__main__":
    unittest.main()
