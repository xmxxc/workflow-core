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
        CREATE TABLE IF NOT EXISTS screen_watch (
            dispatch TEXT PRIMARY KEY, screen TEXT, since REAL, reported_screen TEXT);
    """)
    # `started` is set once the worker's tmux session exists; the watchdog ignores rows without it.
    columns = [c["name"] for c in db.execute("PRAGMA table_info(dispatch)")]
    if "started" not in columns:
        db.execute("ALTER TABLE dispatch ADD COLUMN started REAL")
    # `report_dir` is the directory the worker was given write access to at launch.
    if "report_dir" not in columns:
        db.execute("ALTER TABLE dispatch ADD COLUMN report_dir TEXT")
    return db


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


def worker_brief(dispatch_id, task, report, opening="You are a worker dispatched by the controller"):
    router = f"{shlex.quote(sys.executable)} {shlex.quote(str(ROUTER))}"
    return (f"{opening} (dispatch {dispatch_id}). "
            f"Read and do the task in {shlex.quote(str(task))}. "
            f"When the task is finished, write your report and run: "
            f"{router} done --report {shlex.quote(str(report))}. "
            f"If you need a decision, run: {router} ask \"<question>\" [--options a,b], then end your turn; "
            f"the answer arrives as your next message. Never ask questions in plain text "
            f"or with an interactive question tool; nobody watches this window.")


# Each launcher returns (argv, extra environment) for an unattended worker that may write only
# its cwd plus the `writable` directories (router state and the report's directory), and must be
# able to read its task file in `task_dir`.

def codex_command(brief, writable, task_dir, cwd, model):
    # --no-alt-screen keeps the transcript in the tmux scrollback, so `tmux attach` shows history.
    extra = [arg for d in writable for arg in ("--add-dir", str(d))]
    models = ["-m", model] if model else []
    return ["codex", "--no-alt-screen", "-s", "workspace-write", "-a", "never", *models, *extra, brief], {}


def opencode_command(brief, writable, task_dir, cwd, model):
    # OpenCode's directory rules are tool prompts, not a sandbox (shell writes bypass them), and
    # git or pnpm reach into ~ constantly, so an unattended worker allows every external directory
    # (user decision 2026-11-05). "*" overrides any narrower rule merged in from the user's config.
    config = {"permission": {"external_directory": {"*": "allow"}}}
    models = ["-m", model] if model else []
    return (["opencode", str(cwd), *models, "--prompt", brief],
            {"OPENCODE_CONFIG_CONTENT": json.dumps(config)})


def claude_command(brief, writable, task_dir, cwd, model):
    # The brief goes first: --add-dir is variadic and would swallow a trailing prompt.
    models = ["--model", model] if model else []
    return ["claude", brief, "--permission-mode", "auto", *models, "--add-dir", *map(str, writable)], {}


def claude_config():
    return Path(os.environ.get("WFC_CLAUDE_CONFIG") or Path.home() / ".claude.json")


def trust_claude_worktree(cwd):
    """Claude Code asks for trust on every new path, worktrees included. Pre-trust `cwd` only
    when the user already trusts one of its ancestors (user decision 2026-11-05); otherwise
    leave the prompt for the user. Returns the trusted ancestor, or None."""
    path = claude_config()
    if not path.is_file():
        return None
    config = json.loads(path.read_text())
    projects = config.setdefault("projects", {})
    if projects.get(str(cwd), {}).get("hasTrustDialogAccepted"):
        return str(cwd)
    roots = [p for p, v in projects.items()
             if v.get("hasTrustDialogAccepted") and cwd.is_relative_to(Path(p)) and Path(p) != cwd]
    if not roots:
        return None
    projects[str(cwd)] = {**projects.get(str(cwd), {}), "hasTrustDialogAccepted": True}
    tmp = path.with_name(f".{path.name}.wfc-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    os.replace(tmp, path)
    return max(roots, key=len)


LAUNCHERS = {"codex": codex_command, "opencode": opencode_command, "claude": claude_command}


def cmd_dispatch(args):
    db = connect()
    dispatch_id = "d-" + uuid.uuid4().hex[:8]
    cwd = Path(args.cwd).resolve()
    task = Path(args.task).resolve()
    report = Path(args.report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)  # hosts grant access to existing directories
    command, extra_env = LAUNCHERS[args.host](worker_brief(dispatch_id, task, report),
                                              [state_dir(), report.parent], task.parent, cwd, args.model)
    env = {"WFC_DISPATCH": dispatch_id, "WFC_STATE": str(state_dir()), **extra_env}
    trusted_by = trust_claude_worktree(cwd) if args.host == "claude" else None
    db.execute("INSERT INTO dispatch (id, host, cwd, task, created, report_dir) VALUES (?, ?, ?, ?, ?, ?)",
               (dispatch_id, args.host, str(cwd), str(task), time.time(), str(report.parent)))
    launched = subprocess.run(["tmux", "new-session", "-d", "-s", session_name(dispatch_id), "-c", str(cwd),
                               *[arg for k, v in env.items() for arg in ("-e", f"{k}={v}")],
                               *command])
    if launched.returncode != 0:
        db.execute("DELETE FROM dispatch WHERE id = ?", (dispatch_id,))
        raise SystemExit(f"could not start the worker's tmux session (exit {launched.returncode})")
    db.execute("UPDATE dispatch SET started = ? WHERE id = ?", (time.time(), dispatch_id))
    emit({"dispatch": dispatch_id, "host": args.host, "tmux": session_name(dispatch_id),
          **({"trustedBy": trusted_by} if trusted_by else {})})


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
    if answered(db, question["id"]):
        raise SystemExit(f"question {question['id']} was already answered")
    # A newline would submit a partial answer in the worker's TUI, so the answer travels as one line.
    answer = " ".join(args.answer.splitlines())
    session = session_name(question["dispatch"])
    if not type_into(session, f"[router] Answer to question {question['id']}: {answer}"):
        raise SystemExit(f"could not type the answer into tmux session {session}; is the worker still running?")
    post(db, question["dispatch"], "worker", "reply", {"to": question["id"], "answer": answer})
    emit({"replied": question["id"], "dispatch": question["dispatch"]})


def cmd_continue(args):
    """Give a worker its next task (a fix round, an incremental review) in the same session."""
    db = connect()
    row = db.execute("SELECT * FROM dispatch WHERE id = ? AND started IS NOT NULL", (args.dispatch,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown dispatch {args.dispatch} in {state_dir()}")
    task = Path(args.task).resolve()
    report = Path(args.report).resolve()
    if row["report_dir"] and str(report.parent) != row["report_dir"]:
        raise SystemExit(f"the next report must be in {row['report_dir']}, the directory this worker "
                         f"was given write access to at dispatch")
    session = session_name(args.dispatch)
    if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode != 0:
        raise SystemExit(f"tmux session {session} has ended; dispatch a new worker instead")
    brief = worker_brief(args.dispatch, task, report, opening="[router] New task for you")
    if not type_into(session, brief):
        raise SystemExit(f"could not type the next task into tmux session {session}")
    message_id = post(db, args.dispatch, "worker", "continue", {"task": str(task), "report": str(report)})
    db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (args.dispatch,))
    emit({"continued": args.dispatch, "message": message_id})


def take_controller_messages(db):
    db.execute("BEGIN IMMEDIATE")
    rows = db.execute("SELECT * FROM message WHERE recipient = 'controller' AND delivered = 0 ORDER BY id").fetchall()
    db.executemany("UPDATE message SET delivered = 1 WHERE id = ?", [(r["id"],) for r in rows])
    db.execute("COMMIT")
    return [{"id": r["id"], "dispatch": r["dispatch"], "type": r["type"], **json.loads(r["body"])} for r in rows]


def answered(db, question_id):
    return db.execute("SELECT 1 FROM message WHERE type = 'reply' AND json_extract(body, '$.to') = ?",
                      (question_id,)).fetchone() is not None


def watched(db, dispatch_id):
    """The watchdog watches a started dispatch until it is reported exited, or until it reports
    done for its current task (a `continue` starts a new task)."""
    return db.execute(
        "SELECT 1 FROM dispatch d WHERE d.id = ? AND d.started IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM message m WHERE m.dispatch = d.id AND m.type = 'exited') "
        "AND NOT EXISTS (SELECT 1 FROM message m WHERE m.dispatch = d.id AND m.type = 'done' AND m.id > "
        "  COALESCE((SELECT MAX(c.id) FROM message c WHERE c.dispatch = d.id AND c.type = 'continue'), 0))",
        (dispatch_id,)).fetchone() is not None


def awaiting_answer(db, dispatch_id):
    return any(not answered(db, q["id"]) for q in db.execute(
        "SELECT id FROM message WHERE dispatch = ? AND type = 'question'", (dispatch_id,)))


def check_workers(db, stall_after):
    """Report workers whose tmux session ended, or whose screen stopped changing,
    without `done` or a pending question. A busy TUI redraws a timer or spinner;
    an idle prompt, a crash message or an approval dialog stays still."""
    candidates = [r["id"] for r in db.execute("SELECT id FROM dispatch WHERE started IS NOT NULL")]
    for dispatch_id in candidates:
        if not watched(db, dispatch_id):
            continue
        session = session_name(dispatch_id)
        alive = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0
        captured = subprocess.run(["tmux", "capture-pane", "-p", "-t", session],
                                  capture_output=True, text=True) if alive else None
        now = time.time()
        # Decide and post under one write lock, re-checking state a worker's `done` may have changed.
        db.execute("BEGIN IMMEDIATE")
        try:
            if not watched(db, dispatch_id):
                pass
            elif not alive:
                post(db, dispatch_id, "controller", "exited",
                     {"reason": f"tmux session {session} ended without done"})
            elif awaiting_answer(db, dispatch_id):
                db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (dispatch_id,))
            elif captured.returncode == 0:
                watch_screen(db, dispatch_id, captured.stdout, now, stall_after)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def watch_screen(db, dispatch_id, screen, now, stall_after):
    watch = db.execute("SELECT * FROM screen_watch WHERE dispatch = ?", (dispatch_id,)).fetchone()
    if watch is None or watch["screen"] != screen:
        # A changed screen starts a new episode: the timer restarts and a later stall is reported again.
        db.execute("INSERT OR REPLACE INTO screen_watch (dispatch, screen, since, reported_screen) "
                   "VALUES (?, ?, ?, NULL)", (dispatch_id, screen, now))
    elif now - watch["since"] >= stall_after and watch["reported_screen"] != screen:
        tail = "\n".join([line for line in screen.splitlines() if line.strip()][-15:])
        post(db, dispatch_id, "controller", "stalled",
             {"idleSeconds": int(now - watch["since"]), "screen": tail})
        db.execute("UPDATE screen_watch SET reported_screen = ? WHERE dispatch = ?", (screen, dispatch_id))


def cmd_wait(args):
    db = connect()
    deadline = time.monotonic() + args.timeout
    while True:
        check_workers(db, args.stall_after)
        # At-most-once: messages are marked delivered before they are printed; the ledger is the backstop.
        messages = take_controller_messages(db)
        if messages:
            emit({"messages": messages})
            return 0
        if time.monotonic() >= deadline:
            emit({"messages": [], "timedOut": True})
            return TIMED_OUT
        time.sleep(0.5)  # ponytail: polls SQLite; switch to a FIFO/inotify wake if 0.5 s latency matters


def queue_into_codex(thread, messages):
    """Deliver one batch as a new turn of the Codex controller session (open or closed)."""
    text = "[router] Messages for the controller: " + json.dumps({"messages": messages}, ensure_ascii=False)
    return subprocess.run(["codex", "queue", "--thread", thread, "--message", text, "--resume"],
                          capture_output=True).returncode == 0


def cmd_relay(args):
    """Like `wait`, but runs on and pushes each batch to a Codex controller, which then sleeps
    between turns instead of blocking on `wait`."""
    if args.detach:
        # A Codex controller's shell may end its background jobs; run in a new session instead.
        log = state_dir() / "relay.log"
        state_dir().mkdir(parents=True, exist_ok=True)
        argv = [a for a in sys.argv[1:] if a != "--detach"]
        with open(log, "a") as out:
            child = subprocess.Popen([sys.executable, str(ROUTER), *argv], stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=out, start_new_session=True)
        emit({"pid": child.pid, "log": str(log)})
        return 0
    db = connect()
    deadline = time.monotonic() + args.for_seconds if args.for_seconds else None
    while deadline is None or time.monotonic() < deadline:
        check_workers(db, args.stall_after)
        messages = take_controller_messages(db)
        if messages and not queue_into_codex(args.codex_thread, messages):
            # Put the batch back so the next pass, or a `wait`, delivers it.
            db.executemany("UPDATE message SET delivered = 0 WHERE id = ?", [(m["id"],) for m in messages])
            time.sleep(5)
        time.sleep(0.5)
    return 0


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
    p = sub.add_parser("continue", help="give a worker its next task in the same session")
    p.add_argument("dispatch")
    p.add_argument("--task", required=True)
    p.add_argument("--report", required=True, help="must be in the dispatch's report directory")
    p.set_defaults(func=cmd_continue)
    p = sub.add_parser("wait")
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--stall-after", type=float, default=300,
                   help="seconds a worker's screen may stay unchanged before it is reported as stalled")
    p.set_defaults(func=cmd_wait)
    p = sub.add_parser("relay", help="push controller messages into a Codex controller session")
    p.add_argument("--codex-thread", required=True, help="the Codex controller's session UUID or name")
    p.add_argument("--stall-after", type=float, default=300)
    p.add_argument("--for", dest="for_seconds", type=float, help="stop after this many seconds (default: run on)")
    p.add_argument("--detach", action="store_true",
                   help="run in a new session with output to $WFC_STATE/relay.log; prints the pid")
    p.set_defaults(func=cmd_relay)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
