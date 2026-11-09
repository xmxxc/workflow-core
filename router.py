#!/usr/bin/env python3
"""Route messages between the controller and the workers it dispatches.

The controller starts workers with `dispatch` and collects their messages with
`wait`. A worker reports with `done` or asks with `ask`, then ends its turn;
the controller answers with `reply`, which wakes the worker. State lives in a
SQLite database under $WFC_STATE (default ~/.local/state/workflow-core).
"""

import argparse
import calendar
import fcntl
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
# The program workers call back: this file, or the single-file `wfc` build it is packed into.
PROGRAM = _HERE.parent if _HERE.parent.is_file() else _HERE
TIMED_OUT = 3


def state_dir():
    return Path(os.environ.get("WFC_STATE") or Path.home() / ".local/state/workflow-core").expanduser().resolve()


def tmux(*args):
    """Run a tmux command; a tmux that cannot be run reads as a failed command marked
    `not_run` (it certainly did nothing), not a traceback."""
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True)
    except OSError as err:
        result = subprocess.CompletedProcess(["tmux", *args], 127, "", f"cannot run tmux: {err}")
        result.not_run = True
        return result


def session_alive(session):
    return tmux("has-session", "-t", session).returncode == 0


def session_name(dispatch_id):
    return f"wfc-{dispatch_id}"


# Each table's columns as a fresh database creates them.
TABLES = {
    "dispatch": "id TEXT PRIMARY KEY, host TEXT, cwd TEXT, task TEXT, created REAL",
    "message": "id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch TEXT, recipient TEXT, "
               "type TEXT, body TEXT, created REAL, delivered INTEGER DEFAULT 0",
    "screen_watch": "dispatch TEXT PRIMARY KEY, screen TEXT, since REAL, reported_screen TEXT",
    "host_error": "dispatch TEXT PRIMARY KEY, turn TEXT",
    "waiter": "pid INTEGER PRIMARY KEY, kind TEXT, since REAL",
}
# Column upgrades: `started` is set once the worker's tmux session exists (the watchdog ignores
# rows without it); `report_dir` is the directory the worker may write; `report`, `model` and
# `host_session` are what `resume` needs (Claude Code's session id is set at dispatch), and so are
# `effort` and `network`, the dispatch's reasoning effort and Codex network access.
# `process_start` tells a waiter's own process from a later one that reuses its pid.
ADDED_COLUMNS = {
    "dispatch": {"started": "REAL", "report_dir": "TEXT", "report": "TEXT", "model": "TEXT", "host_session": "TEXT",
                 "effort": "TEXT", "network": "INTEGER"},
    "waiter": {"process_start": "TEXT"},
}


def missing_columns(db):
    """{table: [column, ...]} for every existing table that lacks an added column."""
    missing = {}
    for table, added in ADDED_COLUMNS.items():
        columns = {c["name"] for c in db.execute(f"PRAGMA main.table_info({table})")}
        if columns and set(added) - columns:
            missing[table] = [column for column in added if column not in columns]
    return missing


def connect():
    """Open router.db for a command that writes, creating or upgrading its schema first."""
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state / "router.db", timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript("".join(f"CREATE TABLE IF NOT EXISTS {t} ({cols});" for t, cols in TABLES.items()))
    # Read and alter under one write lock, so parallel first uses do not add a column twice.
    if missing_columns(db):
        db.execute("BEGIN IMMEDIATE")
        try:
            for table, columns in missing_columns(db).items():
                for column in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ADDED_COLUMNS[table][column]}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return db


def connect_read_only():
    """Open router.db without creating or upgrading anything, or None if it does not exist.
    What an older schema lacks reads as empty: a missing table as a TEMP table with no rows, a
    missing column as NULL through a TEMP view (TEMP objects live outside router.db and shadow
    its own). Also returns whether the schema was older than this version."""
    path = state_dir() / "router.db"
    if not path.is_file():
        return None, False
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    existing = {r["name"] for r in db.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
    missing = missing_columns(db)
    for table, cols in TABLES.items():
        if table not in existing:
            added = "".join(f", {column} {kind}" for column, kind in ADDED_COLUMNS.get(table, {}).items())
            db.execute(f"CREATE TEMP TABLE {table} ({cols}{added})")
        elif table in missing:
            nulls = "".join(f", NULL AS {column}" for column in missing[table])
            db.execute(f"CREATE TEMP VIEW {table} AS SELECT *{nulls} FROM main.{table}")
    return db, bool(set(TABLES) - existing or missing)


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


FIRST_BRIEF = "You are a worker dispatched by the controller"


def worker_brief(dispatch_id, task, report, opening=FIRST_BRIEF):
    router = f"{shlex.quote(sys.executable)} {shlex.quote(str(PROGRAM))}"
    return (f"{opening} (dispatch {dispatch_id}). "
            f"Read and do the task in {shlex.quote(str(task))}. "
            f"Stop any server or background process you started before you report. "
            f"When the task is finished, write your report and run: "
            f"{router} done --report {shlex.quote(str(report))}. "
            f"If you need a decision, run: {router} ask \"<question>\" [--options a,b], then end your turn; "
            f"the answer arrives as your next message. Never ask questions in plain text "
            f"or with an interactive question tool; nobody watches this window.")


# Each launcher returns (argv, extra environment) for an unattended worker that may write only
# its cwd plus the `writable` directories (router state and the report's directory). `conversation`
# is the host conversation to resume, or None for a new one (Claude Code: the id to give it).
# `effort` goes to the host's own reasoning-effort option; `network` opens Codex's sandbox to the
# network (the other hosts are not network-sandboxed; see IGNORED_OPTIONS).

def codex_command(brief, writable, cwd, model, conversation=None, resume=False, effort=None, network=False):
    # --no-alt-screen keeps the transcript in the tmux scrollback, so `tmux attach` shows history.
    extra = [arg for d in writable for arg in ("--add-dir", str(d))]
    models = ["-m", model] if model else []
    overrides = [*(["-c", f"model_reasoning_effort={effort}"] if effort else []),
                 *(["-c", "sandbox_workspace_write.network_access=true"] if network else [])]
    flags = ["--no-alt-screen", "-s", "workspace-write", "-a", "never", *models, *overrides, *extra]
    if resume:
        return ["codex", "resume", *flags, conversation, brief], {}
    return ["codex", *flags, brief], {}


def opencode_command(brief, writable, cwd, model, conversation=None, resume=False, effort=None, network=False):
    # OpenCode's directory rules are tool prompts, not a sandbox (shell writes bypass them), and
    # git or pnpm reach into ~ constantly, so an unattended worker allows every external directory
    # (user decision 2026-11-05). "*" overrides any narrower rule merged in from the user's config.
    # OpenCode asks before reading *.env by default; the user allowed it for these test
    # environments (decision 2026-11-07), so an unattended worker is not stopped on that prompt.
    config = {"permission": {"external_directory": {"*": "allow"},
                             "read": {"*.env": "allow", "*.env.*": "allow"}}}
    models = ["-m", model] if model else []
    variant = ["--variant", effort] if effort else []  # OpenCode's provider-specific reasoning effort
    # OpenCode ignores --prompt when it reopens a session; `resume` types the brief in instead.
    start = ["-s", conversation] if resume else ["--prompt", brief]
    return (["opencode", str(cwd), *models, *variant, *start], {"OPENCODE_CONFIG_CONTENT": json.dumps(config)})


def claude_command(brief, writable, cwd, model, conversation=None, resume=False, effort=None, network=False):
    # The brief goes first: --add-dir is variadic and would swallow a trailing prompt.
    models = ["--model", model] if model else []
    efforts = ["--effort", effort] if effort else []
    sessions = ["--resume" if resume else "--session-id", conversation] if conversation else []
    return ["claude", brief, "--permission-mode", "auto", *models, *efforts, *sessions,
            "--add-dir", *map(str, writable)], {}


def find_host_session(row):
    """The host conversation a dispatch ran in: the one whose user message *begins* with this
    dispatch's first brief. Matching the start, not any mention, skips a controller session that
    merely quoted the brief (in a stalled screen, say). Returns None when the host has no record."""
    if row["host_session"]:
        return row["host_session"]
    opening = f"{FIRST_BRIEF} (dispatch {row['id']})"
    if row["host"] == "codex":
        # Codex records each conversation as a rollout whose first line names it. A reviewer's
        # sub-agents inherit the brief, so their rollouts (source.subagent) are skipped.
        root = codex_sessions()
        for rollout in sorted(root.rglob("rollout-*.jsonl")) if root.is_dir() else []:
            try:
                if rollout.stat().st_mtime < (row["created"] or 0) - 60:
                    continue  # last written before this dispatch existed
            except OSError:
                continue  # removed or rotated since it was listed
            found = codex_rollout_opens_with(rollout, opening)
            if found:
                return found
        return None
    if row["host"] == "opencode":
        path = opencode_db()
        if not path.is_file():
            return None
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                found = db.execute(
                    "SELECT s.id FROM session s WHERE s.parent_id IS NULL AND EXISTS "
                    "(SELECT 1 FROM part p WHERE p.session_id = s.id AND substr(json_extract(p.data, '$.text'), 1, ?) = ?) "
                    "ORDER BY s.time_created LIMIT 1", (len(opening), opening)).fetchone()
            finally:
                db.close()
        except sqlite3.Error as err:
            print(f"router: cannot read OpenCode sessions in {path}: {err}", file=sys.stderr)
            return None
        return found[0] if found else None
    return None


def codex_sessions():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"


def opencode_db():
    default = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode/opencode.db"
    return Path(os.environ.get("WFC_OPENCODE_DB") or default).expanduser()


def claude_projects():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "projects"


def iso_time(text):
    """Epoch seconds of a UTC ISO timestamp like 2026-11-08T07:49:19.522Z (Python 3.9 safe)."""
    whole, _, rest = text.partition(".")
    fraction = "".join(c for c in rest.split("Z")[0].split("+")[0] if c.isdigit())
    return calendar.timegm(time.strptime(whole[:19], "%Y-%m-%dT%H:%M:%S")) + float(f"0.{fraction or 0}")


# The host's own record of how a worker's latest turn ended, read from its conversation store
# (internal formats, not public interfaces: anything unreadable counts as no record, and the
# screen watchdog stays the fallback). A turn end is {"turn", "at", "error"}; error is None for a
# turn that ended normally, else {"kind", "message"}. A newer user turn clears it (None).

def codex_turn_end(line, last):
    event = json.loads(line)
    payload = event.get("payload") or {}
    if event.get("type") != "event_msg":
        return last
    if payload.get("type") == "task_started":
        return None
    if payload.get("type") != "task_complete":
        return last
    error = payload.get("error")
    return {"turn": payload.get("turn_id"), "at": iso_time(event["timestamp"]),
            "error": {"kind": error.get("codex_error_info"), "message": error.get("message")} if error else None}


def claude_turn_end(line, last):
    entry = json.loads(line)
    if entry.get("type") == "user":
        return None
    if entry.get("type") != "assistant":
        return last
    error = None
    if entry.get("isApiErrorMessage"):
        content = (entry.get("message") or {}).get("content") or [{}]
        error = {"kind": entry.get("error"), "message": content[0].get("text")}
    return {"turn": entry.get("uuid"), "at": iso_time(entry["timestamp"]), "error": error}


class TurnWatch:
    """Follows one worker's conversation file, reading only what was appended since the last look."""

    LOOKUP_EVERY, CHECK_EVERY = 15, 2

    def __init__(self):
        self.source, self.offset, self.last, self.next_lookup, self.next_check = None, 0, None, 0, 0
        self.identity = None  # (device, inode) of the file read so far

    def latest(self, row, now):
        if now < self.next_check:
            return self.last
        self.next_check = now + self.CHECK_EVERY
        try:
            if self.source is None and now >= self.next_lookup:
                self.next_lookup = now + self.LOOKUP_EVERY
                self.source = conversation_source(row)
            if self.source is None:
                return None
            if row["host"] == "opencode":
                self.last = opencode_turn_end(self.source)
            else:
                self.last = self.follow(codex_turn_end if row["host"] == "codex" else claude_turn_end)
        except FileNotFoundError:
            self.source, self.offset, self.last = None, 0, None  # moved away: look it up again
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, AttributeError, IndexError):
            pass  # unreadable or malformed just now: no new view, and the screen watchdog still runs
        return self.last

    def follow(self, parse):
        with open(self.source, "rb") as f:
            identity = os.fstat(f.fileno())
            if (identity.st_dev, identity.st_ino) != self.identity or identity.st_size < self.offset:
                # A replaced or truncated file is read again from its start.
                self.identity, self.offset, self.last = (identity.st_dev, identity.st_ino), 0, None
            f.seek(self.offset)
            chunk = f.read()
        complete = chunk[:chunk.rfind(b"\n") + 1]  # a half-written last line waits for the next look
        self.offset += len(complete)
        for line in complete.decode(errors="replace").splitlines():
            try:
                self.last = parse(line, self.last)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError):
                continue
        return self.last


def conversation_source(row):
    """Where a worker's conversation is recorded: a Codex rollout or Claude Code transcript file, or
    an OpenCode session id. None until the host has written it."""
    if row["host"] == "opencode":
        return find_host_session(row)
    if row["host"] == "claude":
        found = sorted(claude_projects().glob(f"*/{row['host_session']}.jsonl")) if row["host_session"] else []
        return found[0] if found else None
    session = find_host_session(row)
    found = sorted(codex_sessions().rglob(f"rollout-*{session}.jsonl")) if session else []
    return found[0] if found else None


def opencode_turn_end(session):
    db = sqlite3.connect(f"file:{opencode_db()}?mode=ro", uri=True)
    try:
        found = db.execute("SELECT id, time_created, data FROM message WHERE session_id = ? "
                           "ORDER BY time_created DESC, id DESC LIMIT 1", (session,)).fetchone()
    finally:
        db.close()
    if found is None:
        return None
    data = json.loads(found[2])
    if data.get("role") != "assistant":
        return None
    error = data.get("error")
    return {"turn": found[0], "at": found[1] / 1000,
            "error": {"kind": error.get("name"), "message": (error.get("data") or {}).get("message")} if error else None}


def codex_rollout_opens_with(rollout, opening):
    """The rollout's session id if it is a top-level conversation with a user message that begins
    with `opening`, else None. Reads only up to that message."""
    try:
        with open(rollout, errors="replace") as lines:
            meta = json.loads(next(lines))["payload"]
            if isinstance(meta.get("source"), dict) and "subagent" in meta["source"]:
                return None
            for line in lines:
                if opening not in line:
                    continue
                item = json.loads(line).get("payload", {})
                if item.get("type") == "message" and item.get("role") == "user" and any(
                        str(c.get("text", "")).startswith(opening) for c in item.get("content", [])):
                    return meta["id"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return None
    return None


def claude_config():
    return Path(os.environ.get("WFC_CLAUDE_CONFIG") or Path.home() / ".claude.json")


def trust_claude_worktree(cwd):
    """Claude Code asks for trust on every new path, worktrees included. Pre-trust `cwd` only
    when the user already trusts one of its ancestors (user decision 2026-11-05); otherwise
    leave the prompt for the user. Returns the trusted ancestor, or None.

    A running Claude Code session rewrites this file from memory, so the new entry can be lost
    to a concurrent save; the worker then stops on the trust prompt and is reported `stalled`."""
    path = claude_config().resolve()  # write through a symlinked config to its real file
    if not path.exists():
        return None
    # Serialise the router's own read-modify-write, so parallel dispatches keep each other's entries.
    try:
        lock = os.open(path.with_name(f".{path.name}.wfc.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as err:
        print(f"router: not pre-trusting {cwd}: cannot lock {path}: {err}", file=sys.stderr)
        return None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _trust_locked(path, cwd)
    finally:
        os.close(lock)


def _trust_locked(path, cwd):
    try:
        config = json.loads(path.read_text())
        projects = config.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise ValueError("projects is not an object")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        print(f"router: not pre-trusting {cwd}: cannot read {path}: {err}", file=sys.stderr)
        return None
    if projects.get(str(cwd), {}).get("hasTrustDialogAccepted"):
        return str(cwd)
    roots = [p for p, v in projects.items()
             if isinstance(v, dict) and v.get("hasTrustDialogAccepted")
             and Path(p).resolve() != cwd and cwd.is_relative_to(Path(p).resolve())]
    if not roots:
        return None
    projects[str(cwd)] = {**projects.get(str(cwd), {}), "hasTrustDialogAccepted": True}
    tmp = path.with_name(f".{path.name}.wfc-{os.getpid()}.tmp")
    mode = path.stat().st_mode & 0o777
    try:
        # Created with the config's own mode (fchmod undoes the umask) before any content is written.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), mode)
            out.write(json.dumps(config, indent=2, ensure_ascii=False))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return max(roots, key=len)


LAUNCHERS = {"codex": codex_command, "opencode": opencode_command, "claude": claude_command}
# Dispatch options a host does not need: dispatch accepts them and says so in its output.
IGNORED_OPTIONS = {"opencode": {"network": "--network: OpenCode is not network-sandboxed; ignored"},
                   "claude": {"network": "--network: Claude Code is not network-sandboxed; ignored"}}


def cmd_dispatch(args):
    db = connect()
    dispatch_id = "d-" + uuid.uuid4().hex[:8]
    cwd = Path(args.cwd).resolve()
    task = Path(args.task).resolve()
    report = Path(args.report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)  # hosts grant access to existing directories
    host_session = str(uuid.uuid4()) if args.host == "claude" else None
    command, extra_env = LAUNCHERS[args.host](worker_brief(dispatch_id, task, report),
                                              [state_dir(), report.parent], cwd, args.model, host_session,
                                              effort=args.effort, network=args.network)
    ignored = [note for option, note in IGNORED_OPTIONS.get(args.host, {}).items() if getattr(args, option)]
    trusted_by = trust_claude_worktree(cwd) if args.host == "claude" else None
    db.execute("INSERT INTO dispatch (id, host, cwd, task, created, report_dir, report, model, host_session, "
               "effort, network) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (dispatch_id, args.host, str(cwd), str(task), time.time(), str(report.parent), str(report),
                args.model, host_session, args.effort, int(args.network)))
    launched = launch(dispatch_id, cwd, command, extra_env)
    if launched == NOT_SENT:
        db.execute("DELETE FROM dispatch WHERE id = ?", (dispatch_id,))
        raise SystemExit("could not start the worker's tmux session; is tmux installed and on PATH?")
    db.execute("UPDATE dispatch SET started = ? WHERE id = ?", (time.time(), dispatch_id))
    if launched == UNCERTAIN:
        # Keep the record: a live worker must still be able to report. The watchdog reports
        # `exited` if no session is there.
        raise SystemExit(f"tmux did not confirm the worker's session for dispatch {dispatch_id}; it is kept. "
                         f"Check `tmux ls`; `wait` reports it exited if it never started.")
    emit({"dispatch": dispatch_id, "host": args.host, "tmux": session_name(dispatch_id),
          **({"trustedBy": trusted_by} if trusted_by else {}), **({"ignored": ignored} if ignored else {})})


def launch(dispatch_id, cwd, command, extra_env):
    """Start the worker's tmux session. Returns SENT, NOT_SENT (nothing started) or UNCERTAIN
    (tmux ran but did not confirm: a session may exist)."""
    env = {"WFC_DISPATCH": dispatch_id, "WFC_STATE": str(state_dir()), **extra_env}
    started = tmux("new-session", "-d", "-s", session_name(dispatch_id), "-c", str(cwd),
                   *[arg for k, v in env.items() for arg in ("-e", f"{k}={v}")], *command)
    if started.returncode == 0:
        return SENT
    print(f"router: tmux new-session failed (exit {started.returncode}): {started.stderr.strip()}", file=sys.stderr)
    # Like typing: only a tmux that could not run at all certainly started nothing.
    return NOT_SENT if getattr(started, "not_run", False) else UNCERTAIN


def wait_for_quiet_screen(session, timeout=30):
    """Wait until a just-launched TUI has drawn its prompt: the screen is non-empty and stays the
    same for a second. Returns False if the session ends or never settles."""
    last, deadline = None, time.monotonic() + timeout
    while time.monotonic() < deadline:
        shot = tmux("capture-pane", "-p", "-t", session)
        if shot.returncode != 0:
            return False
        if shot.stdout.strip() and shot.stdout == last:
            return True
        last = shot.stdout
        time.sleep(1)
    return False


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
    dispatch_id = current_dispatch(db)
    # Read before any lock: git may be slow, and it reads the worktree, not router.db.
    head = worktree_head(db.execute("SELECT cwd FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()["cwd"])
    with input_lock(dispatch_id):  # a nudge being typed lands before this done, as recorded
        return finish_round(db, dispatch_id, report, head)


GIT_TIMEOUT = 5


def worktree_head(cwd):
    """{"head": commit, "dirty": bool} of the worker's worktree, for the controller to compare with
    the head its report names. Both are None when git fails, times out or cwd is not a repository."""
    def git(*args):
        # GIT_OPTIONAL_LOCKS=0: `status` must not refresh the index, so done never writes the repo.
        return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                              env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, check=True).stdout
    try:
        head = git("rev-parse", "--verify", "HEAD").strip()
        dirty = bool(git("status", "--porcelain", "--untracked-files=normal").strip())
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None}
    return {"head": head, "dirty": dirty}


def finish_round(db, dispatch_id, report, head):
    db.execute("BEGIN IMMEDIATE")
    current = db.execute("SELECT report FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()["report"]
    # A retried `done` from an earlier round must not finish the round that followed it.
    if current and str(report) != current:
        db.execute("ROLLBACK")
        raise SystemExit(f"{report} is the report of an earlier round; the current round reports to {current}")
    message_id = post(db, dispatch_id, "controller", "done", {"report": str(report), **head})
    db.execute("COMMIT")
    emit({"message": message_id})


def cmd_ask(args):
    db = connect()
    options = [o for o in (args.options or "").split(",") if o]
    message_id = post(db, current_dispatch(db), "controller", "question",
                      {"question": args.question, "options": options})
    emit({"message": message_id, "next": "End your turn now; the answer arrives as your next message."})


SENT, NOT_SENT, UNCERTAIN, PARTIAL = "sent", "not-sent", "uncertain", "partial"


def type_into(session, text):
    """Type one line into a TUI and submit it. Returns
    SENT;
    NOT_SENT when the text certainly did not arrive (tmux could not be run at all): safe to type again;
    UNCERTAIN when the text command ran but did not confirm (it may have handed the text over
      before failing, and a follow-up probe could fail the same way, so none is trusted);
    PARTIAL when the text arrived but its Enter was not confirmed.
    After UNCERTAIN or PARTIAL, typing again could double the line: someone has to look."""
    sent = tmux("send-keys", "-t", session, "-l", text)
    if sent.returncode != 0:
        return NOT_SENT if getattr(sent, "not_run", False) else UNCERTAIN
    time.sleep(0.5)  # TUIs drop an Enter that arrives in the same burst as the text
    return SENT if tmux("send-keys", "-t", session, "Enter").returncode == 0 else PARTIAL


def typing_error(session, what, typed):
    state = ("may or may not have reached" if typed == UNCERTAIN
             else "was typed into, but its Enter was not confirmed in,")
    return SystemExit(f"{what} {state} tmux session {session}. Attach (tmux attach -t {session}), check the input "
                      f"line, and press Enter or clear it yourself; do not send it again.")


class input_lock:
    """Serialises everything that types into one worker's session (continue, nudge and resume from
    their locked round check to their last keystroke; reply while it types), so two inputs never interleave and a round check
    still holds when the text arrives. The worker's own `done` takes it too, so it cannot slip in
    between a nudge's check and its typing."""

    def __init__(self, dispatch_id):
        self.path = state_dir() / f"input-{dispatch_id}.lock"

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def __exit__(self, *exc):
        os.close(self.fd)


def holding_input_lock(command):
    def locked(args):
        connect()  # the state directory exists before its lock file is made
        with input_lock(args.dispatch):
            return command(args)
    locked.__doc__ = command.__doc__
    return locked


def cmd_reply(args):
    db = connect()
    question = db.execute("SELECT * FROM message WHERE id = ? AND type = 'question'", (args.message,)).fetchone()
    if question is None:
        raise SystemExit(f"no question with id {args.message}")
    # A newline would submit a partial answer in the worker's TUI, so the answer travels as one line.
    answer = " ".join(args.answer.splitlines())
    # Claim the question before typing, so two concurrent replies cannot both type an answer.
    db.execute("BEGIN IMMEDIATE")
    if answered(db, question["id"]):
        db.execute("ROLLBACK")
        raise SystemExit(f"question {question['id']} was already answered")
    reply_id = post(db, question["dispatch"], "worker", "reply", {"to": question["id"], "answer": answer})
    db.execute("COMMIT")
    session = session_name(question["dispatch"])
    with input_lock(question["dispatch"]):
        typed = type_into(session, f"[router] Answer to question {question['id']}: {answer}")
    if typed == NOT_SENT:
        db.execute("DELETE FROM message WHERE id = ?", (reply_id,))
        raise SystemExit(f"could not type the answer into tmux session {session}; is the worker still running?")
    if typed in (PARTIAL, UNCERTAIN):
        raise typing_error(session, f"The answer to question {question['id']}", typed)  # the claim stays
    emit({"replied": question["id"], "dispatch": question["dispatch"]})


@holding_input_lock
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
    if watched(db, args.dispatch):
        raise SystemExit(f"dispatch {args.dispatch} has not done its current task yet")
    session = session_name(args.dispatch)
    if not session_alive(session):
        raise SystemExit(f"tmux session {session} has ended; dispatch a new worker instead")
    # Claim the next round under the write lock, re-checking that the current one is done, so two
    # concurrent `continue`s cannot both type a task. Recording first also keeps the new task's
    # `done` above its `continue`.
    db.execute("BEGIN IMMEDIATE")
    if watched(db, args.dispatch) or not done_current_task(db, args.dispatch):
        db.execute("ROLLBACK")
        raise SystemExit(f"dispatch {args.dispatch} has not done its current task yet")
    refuse_used_report(db, args.dispatch, report)
    # What this claim replaces, read under the same lock: a failed send restores exactly this.
    previous = db.execute("SELECT task, report FROM dispatch WHERE id = ?", (args.dispatch,)).fetchone()
    message_id = post(db, args.dispatch, "worker", "continue", {"task": str(task), "report": str(report)})
    db.execute("UPDATE dispatch SET task = ?, report = ? WHERE id = ?", (str(task), str(report), args.dispatch))
    db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (args.dispatch,))
    db.execute("COMMIT")
    brief = worker_brief(args.dispatch, task, report, opening="[router] New task for you")
    typed = type_into(session, brief)
    if typed == NOT_SENT:
        # Undo the claim only while it is still the latest round event and its task is still the
        # current one: a `resume` may have taken the new round over in between.
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT task, report FROM dispatch WHERE id = ?", (args.dispatch,)).fetchone()
        if round_mark(db, args.dispatch) == message_id and (current["task"], current["report"]) == (str(task), str(report)):
            db.execute("DELETE FROM message WHERE id = ?", (message_id,))
            db.execute("UPDATE dispatch SET task = ?, report = ? WHERE id = ?",
                       (previous["task"], previous["report"], args.dispatch))
            db.execute("COMMIT")
            raise SystemExit(f"could not type the next task into tmux session {session}")
        db.execute("COMMIT")
        raise SystemExit(f"could not type the next task into tmux session {session}, but dispatch {args.dispatch} "
                         f"moved on meanwhile (a resume took the round over); its current state is kept")
    if typed in (PARTIAL, UNCERTAIN):
        raise typing_error(session, "The next task", typed)
    emit({"continued": args.dispatch, "message": message_id})


@holding_input_lock
def cmd_nudge(args):
    """Type a line into a worker that has not finished its task (stopped on a quota error, say),
    recorded in router.db."""
    db = connect()
    row = db.execute("SELECT * FROM dispatch WHERE id = ? AND started IS NOT NULL", (args.dispatch,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown dispatch {args.dispatch} in {state_dir()}")
    session = session_name(args.dispatch)
    if not session_alive(session):
        raise SystemExit(f"tmux session {session} has ended; use `resume` instead")
    text = " ".join(args.text.splitlines())  # a newline would submit half the line
    db.execute("BEGIN IMMEDIATE")
    if done_current_task(db, args.dispatch):
        db.execute("ROLLBACK")
        raise SystemExit(f"dispatch {args.dispatch} already finished its task; give the next one with `continue`")
    message_id = post(db, args.dispatch, "worker", "nudge", {"text": text})
    db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (args.dispatch,))
    db.execute("COMMIT")
    typed = type_into(session, f"[router] Message from the controller: {text}")
    if typed == NOT_SENT:
        db.execute("DELETE FROM message WHERE id = ?", (message_id,))
        raise SystemExit(f"could not type the message into tmux session {session}")
    if typed in (PARTIAL, UNCERTAIN):
        raise typing_error(session, "The message", typed)
    emit({"nudged": args.dispatch, "message": message_id})


def local_time(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else None


def message_view(r):
    return {"id": r["id"], "dispatch": r["dispatch"], "type": r["type"], "to": r["recipient"], "at": local_time(r["created"]),
            **({"delivered": bool(r["delivered"])} if r["recipient"] == "controller" else {}), **json.loads(r["body"])}


def dispatch_state(db, dispatch_id):
    if done_current_task(db, dispatch_id):
        return "done"
    if not watched(db, dispatch_id):
        return "exited"
    return "asking" if awaiting_answer(db, dispatch_id) else "working"


def cmd_status(args):
    """Read-only view of the scope: every dispatch, undelivered messages and the running waiters.
    It never creates, upgrades or changes router.db; write commands do that."""
    db, outdated = connect_read_only()
    if db is None:
        if args.dispatch:
            raise SystemExit(f"unknown dispatch {args.dispatch} in {state_dir()}")
        emit({"state": str(state_dir()), "dispatches": [], "undelivered": [], "waiters": []})
        return
    query = "SELECT * FROM dispatch WHERE started IS NOT NULL" + (" AND id = ?" if args.dispatch else "") + " ORDER BY created"
    rows = db.execute(query, (args.dispatch,) if args.dispatch else ()).fetchall()
    if args.dispatch and not rows:
        raise SystemExit(f"unknown dispatch {args.dispatch} in {state_dir()}")
    now, dispatches = time.time(), []
    for row in rows:
        last = db.execute("SELECT * FROM message WHERE dispatch = ? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        watch = db.execute("SELECT since FROM screen_watch WHERE dispatch = ?", (row["id"],)).fetchone()
        view = {"dispatch": row["id"], "host": row["host"], "model": row["model"], "state": dispatch_state(db, row["id"]),
                "tmux": session_name(row["id"]), "tmuxAlive": session_alive(session_name(row["id"])),
                "cwd": row["cwd"], "task": row["task"], "report": row["report"], "started": local_time(row["started"]),
                "last": message_view(last) if last else None,
                **({"screenIdleSeconds": int(now - watch["since"])} if watch else {})}
        if args.dispatch:
            view["messages"] = [message_view(m) for m in
                                db.execute("SELECT * FROM message WHERE dispatch = ? ORDER BY id", (row["id"],))]
        dispatches.append(view)
    undelivered = db.execute("SELECT * FROM message WHERE recipient = 'controller' AND delivered = 0 "
                             + ("AND dispatch = ? " if args.dispatch else "") + "ORDER BY id",
                             (args.dispatch,) if args.dispatch else ()).fetchall()
    waiters = [{"pid": w["pid"], "kind": w["kind"], "since": local_time(w["since"])}
               for w in db.execute("SELECT * FROM waiter ORDER BY since") if waiter_alive(w)]
    emit({"state": str(state_dir()), "dispatches": dispatches,
          "undelivered": [message_view(m) for m in undelivered], "waiters": waiters,
          **({"schema": "router.db is older than this wfc and is shown as far as it goes; "
                        "the next write command (wait, dispatch, ...) upgrades it"} if outdated else {})})


def waiter_alive(row):
    """A row registered with its process start names that one process; an older row has only its pid."""
    if row["process_start"]:
        return process_start(row["pid"]) == row["process_start"]
    return process_alive(row["pid"])


def process_start(pid):
    """When process `pid` started, as `ps` prints it, or None when there is no such process.
    With the pid it names one process, so a reused pid does not pass for a gone one."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except OSError:
        return None
    return result.stdout.strip() or None


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class registered_waiter:
    """Records a running `wait` or `relay` in router.db for `status`, and removes it on exit."""

    def __init__(self, db, kind):
        self.db, self.kind = db, kind

    def __enter__(self):
        self.db.execute("INSERT OR REPLACE INTO waiter (pid, kind, since, process_start) VALUES (?, ?, ?, ?)",
                        (os.getpid(), self.kind, time.time(), process_start(os.getpid())))

    def __exit__(self, *exc):
        self.db.execute("DELETE FROM waiter WHERE pid = ?", (os.getpid(),))


def cmd_resume(args):
    """Relaunch a worker whose tmux session ended into its own host conversation, either to finish
    its current task or, once that is done, to start the next one (like `continue`)."""
    db = connect()
    row = db.execute("SELECT * FROM dispatch WHERE id = ? AND started IS NOT NULL", (args.dispatch,)).fetchone()
    if row is None:
        raise SystemExit(f"unknown dispatch {args.dispatch} in {state_dir()}")
    mark = round_mark(db, args.dispatch)
    session = session_name(args.dispatch)
    if session_alive(session):
        raise SystemExit(f"tmux session {session} is still running; use `continue` or attach to it")
    if bool(args.task) != bool(args.report):
        raise SystemExit("give both --task and --report, or neither")
    if not row["report_dir"]:
        raise SystemExit(f"dispatch {args.dispatch} predates resume support; dispatch a new worker")
    if args.task and not done_current_task(db, args.dispatch):
        raise SystemExit(f"dispatch {args.dispatch} has not done its current task; resume without --task to finish it")
    if args.task:
        task, report = Path(args.task).resolve(), Path(args.report).resolve()
        if row["report_dir"] and str(report.parent) != row["report_dir"]:
            raise SystemExit(f"the next report must be in {row['report_dir']}, the directory this worker "
                             f"was given write access to at dispatch")
    elif done_current_task(db, args.dispatch):
        raise SystemExit(f"dispatch {args.dispatch} already finished its task; give the next one with --task and --report")
    elif not row["report"]:
        raise SystemExit(f"dispatch {args.dispatch} predates resume support; give its task with --task and --report")
    else:
        task, report = Path(row["task"]), Path(row["report"])
    host_session = find_host_session(row)
    if not host_session:
        raise SystemExit(f"cannot find the {row['host']} conversation of dispatch {args.dispatch}; dispatch a new worker")
    opening = ("[router] Your session was interrupted and has been resumed. Check the worktree and what you "
               "already did, then continue" if not args.task else "[router] Resumed with a new task for you")
    command, extra_env = LAUNCHERS[row["host"]](worker_brief(args.dispatch, task, report, opening=opening),
                                                [state_dir(), Path(row["report_dir"])], Path(row["cwd"]),
                                                row["model"], host_session, resume=True,
                                                effort=row["effort"], network=bool(row["network"]))
    # From the locked re-check to the last keystroke under the input lock (taken before the SQLite
    # lock, as everywhere), so no nudge, done or continue can come between the launch and the brief.
    with input_lock(args.dispatch):
        # Posted before launch so the new session's `done` sorts after it, and under the write lock the
        # watchdog also takes, so it cannot report the not-yet-started session as exited in between.
        db.execute("BEGIN IMMEDIATE")
        # Everything above was decided without the lock: give up if the round moved on meanwhile
        # (another resume, a continue, a done) or a session came back.
        # The row read before the lock must still be the current one, as must the round mark: an old
        # row paired with a newer mark would relaunch the wrong round.
        now = db.execute("SELECT task, report, host_session FROM dispatch WHERE id = ?", (args.dispatch,)).fetchone()
        if (round_mark(db, args.dispatch) != mark or session_alive(session)
                or (now["task"], now["report"], now["host_session"]) != (row["task"], row["report"], row["host_session"])):
            db.execute("ROLLBACK")
            raise SystemExit(f"dispatch {args.dispatch} changed while resuming (another resume, continue or done); "
                             f"look at its state and run resume again if it is still needed")
        if args.task:
            refuse_used_report(db, args.dispatch, report)
        post(db, args.dispatch, "worker", "resumed", {"session": host_session})
        if args.task:
            post(db, args.dispatch, "worker", "continue", {"task": str(task), "report": str(report)})
        db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (args.dispatch,))
        launched = launch(args.dispatch, row["cwd"], command, extra_env)
        if launched == NOT_SENT:
            db.execute("ROLLBACK")
            raise SystemExit(f"could not start tmux session {session}")
        # The session (possibly) runs this task: record it with the launch, before typing can fail.
        db.execute("UPDATE dispatch SET task = ?, report = ?, host_session = ? WHERE id = ?",
                   (str(task), str(report), host_session, args.dispatch))
        db.execute("COMMIT")
        if launched == UNCERTAIN:
            raise SystemExit(f"tmux did not confirm {session}; the resumed round is recorded. Check `tmux ls`; "
                             f"`wait` reports it exited if it never started.")
        if row["host"] == "opencode":
            brief = worker_brief(args.dispatch, task, report, opening=opening)
            typed = type_into(session, brief) if wait_for_quiet_screen(session) else NOT_SENT
            if typed in (PARTIAL, UNCERTAIN):
                raise typing_error(session, "The brief", typed)
            if typed == NOT_SENT:
                raise SystemExit(f"resumed {session} but could not type the brief into it; attach and paste it: {brief}")
    emit({"resumed": args.dispatch, "session": host_session, "tmux": session})


def take_controller_messages(db):
    db.execute("BEGIN IMMEDIATE")
    rows = db.execute("SELECT * FROM message WHERE recipient = 'controller' AND delivered = 0 ORDER BY id").fetchall()
    db.executemany("UPDATE message SET delivered = 1 WHERE id = ?", [(r["id"],) for r in rows])
    db.execute("COMMIT")
    return [{"id": r["id"], "dispatch": r["dispatch"], "type": r["type"], **json.loads(r["body"])} for r in rows]


def answered(db, question_id):
    return db.execute("SELECT 1 FROM message WHERE type = 'reply' AND json_extract(body, '$.to') = ?",
                      (question_id,)).fetchone() is not None


def used_reports(db, dispatch_id):
    """Every report path this dispatch has been given: its current one and each finished round's.
    (Every earlier round finished with `done`, since a next round needs one.)"""
    current = db.execute("SELECT report FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()["report"]
    finished = {json.loads(r["body"])["report"] for r in
                db.execute("SELECT body FROM message WHERE dispatch = ? AND type = 'done'", (dispatch_id,))}
    return finished | ({current} if current else set())


def refuse_used_report(db, dispatch_id, report):
    """A reused report path would let an earlier round's `done` finish the new round."""
    if str(report) in used_reports(db, dispatch_id):
        db.execute("ROLLBACK")
        raise SystemExit(f"{report} was already used by an earlier round of {dispatch_id}; "
                         f"give each round its own report file (for example r<N>.md)")


def round_mark(db, dispatch_id):
    """The latest event that moves a dispatch's round on; a change means the round moved."""
    return db.execute("SELECT MAX(id) FROM message WHERE dispatch = ? AND type IN ('continue', 'resumed', 'done')",
                      (dispatch_id,)).fetchone()[0]


def done_current_task(db, dispatch_id):
    """True once the dispatch reported done after its latest `continue` (or its first task)."""
    return db.execute(
        "SELECT 1 FROM message m WHERE m.dispatch = ? AND m.type = 'done' AND m.id > "
        "COALESCE((SELECT MAX(c.id) FROM message c WHERE c.dispatch = m.dispatch AND c.type = 'continue'), 0)",
        (dispatch_id,)).fetchone() is not None


def watched(db, dispatch_id):
    """The watchdog watches a started dispatch until it is reported exited (since its latest
    `resume`), or until it reports done for its current task (a `continue` starts a new task)."""
    started = db.execute("SELECT 1 FROM dispatch WHERE id = ? AND started IS NOT NULL", (dispatch_id,)).fetchone()
    exited = db.execute(
        "SELECT 1 FROM message m WHERE m.dispatch = ? AND m.type = 'exited' AND m.id > "
        "COALESCE((SELECT MAX(r.id) FROM message r WHERE r.dispatch = m.dispatch AND r.type = 'resumed'), 0)",
        (dispatch_id,)).fetchone()
    return started is not None and exited is None and not done_current_task(db, dispatch_id)


def awaiting_answer(db, dispatch_id):
    return any(not answered(db, q["id"]) for q in db.execute(
        "SELECT id FROM message WHERE dispatch = ? AND type = 'question'", (dispatch_id,)))


def check_workers(db, stall_after):
    """Report workers whose tmux session ended, or whose screen stopped changing,
    without `done` or a pending question. A busy TUI redraws a timer or spinner;
    an idle prompt, a crash message or an approval dialog stays still."""
    candidates = db.execute("SELECT * FROM dispatch WHERE started IS NOT NULL").fetchall()
    for row in candidates:
        dispatch_id = row["id"]
        if not watched(db, dispatch_id):
            continue
        session = session_name(dispatch_id)
        alive = session_alive(session)
        captured = tmux("capture-pane", "-p", "-t", session) if alive else None
        now = time.time()
        # A resume starts a fresh watch: the host may record the reopened conversation elsewhere.
        watch_key = (dispatch_id, db.execute("SELECT MAX(id) FROM message WHERE dispatch = ? AND type = 'resumed'",
                                             (dispatch_id,)).fetchone()[0])
        turn_end = TURN_WATCHES.setdefault(watch_key, TurnWatch()).latest(row, now) if alive else None
        # Decide and post under one write lock, re-checking state a worker's `done` may have changed.
        db.execute("BEGIN IMMEDIATE")
        try:
            if not watched(db, dispatch_id):
                pass
            elif not alive and session_alive(session):
                pass  # a stale sample: `resume` relaunched it while we sampled; look again next pass
            elif not alive:
                post(db, dispatch_id, "controller", "exited",
                     {"reason": f"tmux session {session} ended without done"})
            elif awaiting_answer(db, dispatch_id):
                db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (dispatch_id,))
            elif blocked_turn(db, dispatch_id, turn_end):
                post(db, dispatch_id, "controller", "blocked", {"host": row["host"], **turn_end["error"]})
                db.execute("INSERT OR REPLACE INTO host_error (dispatch, turn) VALUES (?, ?)",
                           (dispatch_id, turn_end["turn"]))
                if captured.returncode == 0:  # the stall that follows on this screen is the same news
                    db.execute("INSERT OR REPLACE INTO screen_watch (dispatch, screen, since, reported_screen) "
                               "VALUES (?, ?, ?, ?)", (dispatch_id, captured.stdout, now, captured.stdout))
            elif captured.returncode == 0:
                watch_screen(db, dispatch_id, captured.stdout, now, stall_after)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


TURN_WATCHES = {}


def blocked_turn(db, dispatch_id, turn_end):
    """The worker's latest turn ended on a host error (quota, overload, policy) in its current
    round, not yet reported."""
    if not turn_end or not turn_end["error"]:
        return False
    reported = db.execute("SELECT turn FROM host_error WHERE dispatch = ?", (dispatch_id,)).fetchone()
    return (reported is None or reported["turn"] != turn_end["turn"]) and turn_end["at"] > round_started(db, dispatch_id)


def round_started(db, dispatch_id):
    """When the worker last got input from the router: its dispatch, or its latest continue,
    resume or nudge. A host error from before that belongs to an earlier turn."""
    latest = db.execute("SELECT MAX(created) FROM message WHERE dispatch = ? AND type IN ('continue', 'resumed', 'nudge')",
                        (dispatch_id,)).fetchone()[0]
    started = db.execute("SELECT started FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()[0]
    return max(latest or 0, started or 0)


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
    with registered_waiter(db, "wait"):
        while True:
            check_workers(db, args.stall_after)
            # At-most-once: messages are marked delivered before they are printed; `status` shows them again.
            messages = take_controller_messages(db)
            if messages:
                emit({"messages": messages})
                return 0
            if time.monotonic() >= deadline:
                emit({"messages": [], "timedOut": True})
                return TIMED_OUT
            time.sleep(0.5)  # ponytail: polls SQLite; switch to a FIFO/inotify wake if 0.5 s latency matters


def queue_into_codex(thread, messages):
    """Deliver one batch as a new turn of the Codex controller session (open or closed).
    Returns "delivered", "failed" (codex could not even start: safe to retry) or "uncertain"
    (codex ran but did not confirm, e.g. a timeout or a non-zero exit that may follow
    acceptance: not retried, so a batch is never queued twice)."""
    text = "[router] Messages for the controller: " + json.dumps({"messages": messages}, ensure_ascii=False)
    ids = [m["id"] for m in messages]
    try:
        result = subprocess.run(["codex", "queue", "--thread", thread, "--message", text, "--resume"],
                                capture_output=True, text=True, timeout=float(os.environ.get("WFC_QUEUE_TIMEOUT", 60)))
    except OSError as err:
        print(f"router relay: codex queue could not run: {err}", flush=True)
        return "failed"
    except subprocess.TimeoutExpired:
        print(f"router relay: codex queue timed out; delivery of messages {ids} is uncertain and not retried. "
              f"Check the controller session; the messages stay in router.db.", flush=True)
        return "uncertain"
    if result.returncode != 0:
        print(f"router relay: codex queue exited {result.returncode}: {result.stderr.strip()}; delivery of "
              f"messages {ids} is uncertain and not retried. Check the controller session; the messages stay "
              f"in router.db.", flush=True)
        return "uncertain"
    return "delivered"


class _KeepRedirects(urllib.request.HTTPRedirectHandler):
    """Report a redirect as the HTTPError it is: the first endpoint may already have taken the POST,
    so following it could resend the batch or take an unrelated answer for delivery."""

    def redirect_request(self, *args, **kwargs):
        return None


NO_REDIRECTS = urllib.request.build_opener(_KeepRedirects)


def post_into_opencode(url, session, messages):
    """Deliver one batch as a new turn of an OpenCode controller session through its HTTP API
    (the TUI or `opencode serve` started with a fixed --port). Same outcomes as queue_into_codex:
    "failed" only when OpenCode certainly did not take it (nothing listening), else "uncertain"
    unless it answered 2xx."""
    text = "[router] Messages for the controller: " + json.dumps({"messages": messages}, ensure_ascii=False)
    ids = [m["id"] for m in messages]
    request = urllib.request.Request(f"{url.rstrip('/')}/session/{session}/prompt_async", method="POST",
                                     data=json.dumps({"parts": [{"type": "text", "text": text}]}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with NO_REDIRECTS.open(request, timeout=float(os.environ.get("WFC_QUEUE_TIMEOUT", 60))):
            return "delivered"
    except urllib.error.HTTPError as err:
        reason = f"OpenCode answered {err.code}"
    except urllib.error.URLError as err:
        if isinstance(err.reason, ConnectionRefusedError):
            print(f"router relay: nothing listens at {url}; messages {ids} stay waiting", flush=True)
            return "failed"
        reason = f"request failed: {err.reason}"
    except (OSError, TimeoutError) as err:
        reason = f"request failed: {err}"
    print(f"router relay: {reason}; delivery of messages {ids} is uncertain and not retried. Check the "
          f"controller session; the messages stay in router.db.", flush=True)
    return "uncertain"


def relay_target(args):
    """The controller to push into: a Codex thread, or an OpenCode session at a URL."""
    opencode = bool(args.opencode_url) or bool(args.opencode_session)
    if bool(args.codex_thread) == opencode or (opencode and not (args.opencode_url and args.opencode_session)):
        raise SystemExit("give either --codex-thread, or both --opencode-url and --opencode-session")
    if opencode and not args.opencode_url.startswith(("http://", "https://")):
        raise SystemExit("--opencode-url must be an http(s) URL, e.g. http://127.0.0.1:4096")
    if args.codex_thread:
        return ["--codex-thread", args.codex_thread], lambda batch: queue_into_codex(args.codex_thread, batch)
    return (["--opencode-url", args.opencode_url, "--opencode-session", args.opencode_session],
            lambda batch: post_into_opencode(args.opencode_url, args.opencode_session, batch))


def cmd_relay(args):
    """Like `wait`, but runs on and pushes each batch to a Codex or OpenCode controller, which then
    sleeps between turns instead of blocking on `wait`."""
    target_args, deliver = relay_target(args)
    if args.detach:
        # A Codex controller's shell may end its background jobs; run in a new session instead.
        log = state_dir() / "relay.log"
        state_dir().mkdir(parents=True, exist_ok=True)
        argv = ["relay", *target_args, "--stall-after", str(args.stall_after),
                *(["--for", str(args.for_seconds)] if args.for_seconds else [])]
        with open(log, "a") as out:
            child = subprocess.Popen([sys.executable, str(PROGRAM), *argv], stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=out, start_new_session=True)
        emit({"pid": child.pid, "log": str(log)})
        return 0
    db = connect()
    deadline = time.monotonic() + args.for_seconds if args.for_seconds else None
    with registered_waiter(db, "relay"):
        while deadline is None or time.monotonic() < deadline:
            check_workers(db, args.stall_after)
            messages = take_controller_messages(db)
            if messages and deliver(messages) == "failed":
                # Put the batch back so the next pass, or a `wait`, delivers it.
                db.executemany("UPDATE message SET delivered = 0 WHERE id = ?", [(m["id"],) for m in messages])
                time.sleep(5)
            time.sleep(0.5)
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog="router.py", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser = sub.add_parser
    sub.add_parser = lambda *a, **kw: add_parser(*a, allow_abbrev=False, **kw)  # `--deta` must not mean --detach
    p = sub.add_parser("dispatch")
    p.add_argument("--host", required=True, choices=sorted(LAUNCHERS))
    p.add_argument("--task", required=True)
    p.add_argument("--cwd", required=True)
    p.add_argument("--report", required=True, help="report path; its directory is made writable for the worker")
    p.add_argument("--model", help="model for the worker; passed to the host's own model flag")
    p.add_argument("--effort", help="reasoning effort, passed to the host's own option "
                                    "(Codex model_reasoning_effort, Claude Code --effort, OpenCode --variant)")
    p.add_argument("--network", action="store_true",
                   help="let a Codex worker's sandbox reach the network (other hosts are not sandboxed)")
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
    p = sub.add_parser("resume", help="relaunch a worker whose session ended into its own conversation")
    p.add_argument("dispatch")
    p.add_argument("--task", help="the next task, for a worker that already finished its current one")
    p.add_argument("--report", help="the next report; must be in the dispatch's report directory")
    p.set_defaults(func=cmd_resume)
    p = sub.add_parser("nudge", help="type a line into a worker that has not finished its task")
    p.add_argument("dispatch")
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_nudge)
    p = sub.add_parser("status", help="show every dispatch, undelivered messages and running waiters")
    p.add_argument("--dispatch", help="one dispatch, with its whole message history")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("wait")
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--stall-after", type=float, default=300,
                   help="seconds a worker's screen may stay unchanged before it is reported as stalled")
    p.set_defaults(func=cmd_wait)
    p = sub.add_parser("relay", help="push controller messages into a Codex or OpenCode controller session")
    p.add_argument("--codex-thread", help="the Codex controller's session UUID or name")
    p.add_argument("--opencode-url", help="the OpenCode controller's server, e.g. http://127.0.0.1:4096")
    p.add_argument("--opencode-session", help="the OpenCode controller's session id (ses_...)")
    p.add_argument("--stall-after", type=float, default=300)
    p.add_argument("--for", dest="for_seconds", type=float, help="stop after this many seconds (default: run on)")
    p.add_argument("--detach", action="store_true",
                   help="run in a new session with output to $WFC_STATE/relay.log; prints the pid")
    p.set_defaults(func=cmd_relay)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
