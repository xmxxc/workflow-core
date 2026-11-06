#!/usr/bin/env python3
"""Wake the controller window when a worker or reviewer finishes a turn.

A worker's completion hook calls `notify_controller.py hook <claude|codex|opencode>`
and passes the hook payload on stdin. The worker's directory holds
`.workflow-core/task.json`, which names the ticket, the report path and the
controller to wake.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

TASK_FILE = Path(".workflow-core") / "task.json"


def find_task(start):
    for directory in [start, *start.parents]:
        candidate = directory / TASK_FILE
        if candidate.is_file():
            return candidate
    return None


def deliver_claude(controller, event):
    events = Path(controller["events_dir"])
    events.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{event['ticket']}-{event['kind']}.json"
    tmp = events / f".{name}.tmp"
    tmp.write_text(json.dumps(event))
    os.replace(tmp, events / name)


def event_text(event):
    return "[workflow-core] " + json.dumps(event, ensure_ascii=False)


def deliver_codex(controller, event):
    subprocess.run(["codex", "queue", "--thread", controller["thread"],
                    "--message", event_text(event), "--resume"],
                   check=True, stdout=subprocess.DEVNULL)


def deliver(controller, event):
    {"claude": deliver_claude, "codex": deliver_codex}[controller["host"]](controller, event)


def structured_question(payload):
    """Question text when the worker asked through its host's question channel, else None."""
    event = payload.get("hook_event_name")
    if event == "Notification":  # Claude Code: AskUserQuestion or a permission prompt
        return payload.get("message") or ""
    if event == "question.asked":  # OpenCode question tool, forwarded by its plugin
        return "\n".join(q.get("question", "") for q in payload.get("questions", []))
    return None


def hook(host, payload):
    if payload.get("background_tasks"):
        return 0
    task_path = find_task(Path(payload.get("cwd") or os.getcwd()))
    if task_path is None:
        return 0
    task = json.loads(task_path.read_text())
    report = Path(task["report"])
    base = {"ticket": task["ticket"], "report": str(report), "worker_host": host}

    question = structured_question(payload)
    if question is not None:
        deliver(task["controller"], {**base, "kind": "needs-person", "question": question})
        return 0
    if not report.is_file():
        deliver(task["controller"], {
            **base, "kind": "needs-person", "question": payload.get("last_assistant_message") or ""})
        return 0
    state_path = task_path.with_name("notified.json")
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    if state.get("report_sha256") == digest:
        return 0
    deliver(task["controller"], {**base, "kind": "done"})
    state_path.write_text(json.dumps({**state, "report_sha256": digest}))
    return 0


def main(argv):
    if len(argv) == 2 and argv[0] == "hook":
        return hook(argv[1], json.loads(sys.stdin.read() or "{}"))
    print("usage: notify_controller.py hook <claude|codex|opencode>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
