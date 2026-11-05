#!/usr/bin/env python3
"""Route messages between the controller and the workers it dispatches.

The controller starts workers with `dispatch` and collects their messages with
`wait`. A worker reports with `done` or asks with `ask`, then ends its turn;
the controller answers with `reply`, which wakes the worker. State lives in a
SQLite database under $WFC_STATE (default ~/.local/state/workflow-core).
"""

import argparse
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROUTER = Path(__file__).resolve()
TIMED_OUT = 3


def state_dir():
    return Path(os.environ.get("WFC_STATE") or Path.home() / ".local/state/workflow-core").expanduser().resolve()


def session_name(dispatch_id):
    return f"wfc-{dispatch_id}"


def connect():
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state / "router.db", timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS dispatch (
            id TEXT PRIMARY KEY, host TEXT, cwd TEXT, task TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS message (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch TEXT, recipient TEXT,
            type TEXT, body TEXT, created REAL, delivered INTEGER DEFAULT 0);
    """)
    return db


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


def worker_brief(dispatch_id, task, report):
    router = f"{shlex.quote(sys.executable)} {shlex.quote(str(ROUTER))}"
    return (f"You are a worker dispatched by the controller (dispatch {dispatch_id}). "
            f"Read and do the task in {shlex.quote(str(task))}. "
            f"When the task is finished, write your report and run: "
            f"{router} done --report {shlex.quote(str(report))}. "
            f"If you need a decision, run: {router} ask \"<question>\" [--options a,b], then end your turn; "
            f"the answer arrives as your next message. Never ask questions in plain text "
            f"or with an interactive question tool; nobody watches this window.")


# Each launcher returns (argv, extra environment) for an unattended worker that may write only
# its cwd plus the `writable` directories (router state and the report's directory).

def codex_command(brief, writable, cwd, model):
    # --no-alt-screen keeps the transcript in the tmux scrollback, so `tmux attach` shows history.
    extra = [arg for d in writable for arg in ("--add-dir", str(d))]
    models = ["-m", model] if model else []
    return ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "never", *models, *extra, brief], {}


def opencode_command(brief, writable, cwd, model):
    # Paths outside the cwd default to "ask"; allow exactly the writable dirs instead of --auto.
    # OpenCode merges this over the user's config, so "*": "ask" overrides any broader rule there.
    config = {"permission": {"external_directory": {"*": "ask", **{f"{d}/**": "allow" for d in writable}}}}
    models = ["-m", model] if model else []
    return (["opencode", str(cwd), *models, "--prompt", brief],
            {"OPENCODE_CONFIG_CONTENT": json.dumps(config)})


def claude_command(brief, writable, cwd, model):
    # The brief goes first: --add-dir is variadic and would swallow a trailing prompt.
    models = ["--model", model] if model else []
    return ["claude", brief, "--permission-mode", "auto", *models, "--add-dir", *map(str, writable)], {}


LAUNCHERS = {"codex": codex_command, "opencode": opencode_command, "claude": claude_command}


def cmd_dispatch(args):
    db = connect()
    dispatch_id = "d-" + uuid.uuid4().hex[:8]
    cwd = Path(args.cwd).resolve()
    task = Path(args.task).resolve()
    report = Path(args.report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)  # hosts grant access to existing directories
    command, extra_env = LAUNCHERS[args.host](worker_brief(dispatch_id, task, report),
                                              [state_dir(), report.parent], cwd, args.model)
    env = {"WFC_DISPATCH": dispatch_id, "WFC_STATE": str(state_dir()), **extra_env}
    db.execute("INSERT INTO dispatch VALUES (?, ?, ?, ?, ?)",
               (dispatch_id, args.host, str(cwd), str(task), time.time()))
    launched = subprocess.run(["tmux", "new-session", "-d", "-s", session_name(dispatch_id), "-c", str(cwd),
                               *[arg for k, v in env.items() for arg in ("-e", f"{k}={v}")],
                               *command])
    if launched.returncode != 0:
        db.execute("DELETE FROM dispatch WHERE id = ?", (dispatch_id,))
        raise SystemExit(f"could not start the worker's tmux session (exit {launched.returncode})")
    emit({"dispatch": dispatch_id, "host": args.host, "tmux": session_name(dispatch_id)})


def current_dispatch(db):
    dispatch_id = os.environ.get("WFC_DISPATCH")
    if not dispatch_id:
        raise SystemExit("WFC_DISPATCH is not set; run this inside a dispatched worker")
    if db.execute("SELECT 1 FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone() is None:
        raise SystemExit(f"unknown dispatch {dispatch_id} in {state_dir()}")
    return dispatch_id


def post(db, dispatch_id, recipient, type_, body):
    cur = db.execute("INSERT INTO message (dispatch, recipient, type, body, created) VALUES (?, ?, ?, ?, ?)",
                     (dispatch_id, recipient, type_, json.dumps(body, ensure_ascii=False), time.time()))
    return cur.lastrowid


def cmd_done(args):
    report = Path(args.report).resolve()
    if not report.is_file():
        raise SystemExit(f"report not found: {report}")
    db = connect()
    message_id = post(db, current_dispatch(db), "controller", "done", {"report": str(report)})
    emit({"message": message_id})


def cmd_ask(args):
    db = connect()
    options = [o for o in (args.options or "").split(",") if o]
    message_id = post(db, current_dispatch(db), "controller", "question",
                      {"question": args.question, "options": options})
    emit({"message": message_id, "next": "End your turn now; the answer arrives as your next message."})


def type_into(session, text):
    """Type one line into a TUI and submit it. Returns False if tmux could not deliver it."""
    if subprocess.run(["tmux", "send-keys", "-t", session, "-l", text]).returncode != 0:
        return False
    time.sleep(0.5)  # TUIs drop an Enter that arrives in the same burst as the text
    return subprocess.run(["tmux", "send-keys", "-t", session, "Enter"]).returncode == 0


def cmd_reply(args):
    db = connect()
    question = db.execute("SELECT * FROM message WHERE id = ? AND type = 'question'", (args.message,)).fetchone()
    if question is None:
        raise SystemExit(f"no question with id {args.message}")
    answered = db.execute("SELECT 1 FROM message WHERE type = 'reply' AND json_extract(body, '$.to') = ?",
                          (question["id"],)).fetchone()
    if answered:
        raise SystemExit(f"question {question['id']} was already answered")
    # A newline would submit a partial answer in the worker's TUI, so the answer travels as one line.
    answer = " ".join(args.answer.splitlines())
    session = session_name(question["dispatch"])
    if not type_into(session, f"[router] Answer to question {question['id']}: {answer}"):
        raise SystemExit(f"could not type the answer into tmux session {session}; is the worker still running?")
    post(db, question["dispatch"], "worker", "reply", {"to": question["id"], "answer": answer})
    emit({"replied": question["id"], "dispatch": question["dispatch"]})


def take_controller_messages(db):
    db.execute("BEGIN IMMEDIATE")
    rows = db.execute("SELECT * FROM message WHERE recipient = 'controller' AND delivered = 0 ORDER BY id").fetchall()
    db.executemany("UPDATE message SET delivered = 1 WHERE id = ?", [(r["id"],) for r in rows])
    db.execute("COMMIT")
    return [{"id": r["id"], "dispatch": r["dispatch"], "type": r["type"], **json.loads(r["body"])} for r in rows]


def cmd_wait(args):
    db = connect()
    deadline = time.monotonic() + args.timeout
    while True:
        # At-most-once: messages are marked delivered before they are printed; the ledger is the backstop.
        messages = take_controller_messages(db)
        if messages:
            emit({"messages": messages})
            return 0
        if time.monotonic() >= deadline:
            emit({"messages": [], "timedOut": True})
            return TIMED_OUT
        time.sleep(0.5)  # ponytail: polls SQLite; switch to a FIFO/inotify wake if 0.5 s latency matters


def main(argv):
    parser = argparse.ArgumentParser(prog="router.py")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("dispatch")
    p.add_argument("--host", required=True, choices=sorted(LAUNCHERS))
    p.add_argument("--task", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--report", required=True, help="report path; its directory is made writable for the worker")
    p.add_argument("--model", help="model for the worker; passed to the host's own model flag")
    p.set_defaults(func=cmd_dispatch)
    p = sub.add_parser("done")
    p.add_argument("--report", required=True)
    p.set_defaults(func=cmd_done)
    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--options")
    p.set_defaults(func=cmd_ask)
    p = sub.add_parser("reply")
    p.add_argument("message", type=int)
    p.add_argument("answer")
    p.set_defaults(func=cmd_reply)
    p = sub.add_parser("wait")
    p.add_argument("--timeout", type=float, default=3600)
    p.set_defaults(func=cmd_wait)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
