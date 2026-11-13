#!/usr/bin/env python3
"""Route messages between the controller and the workers it dispatches.

The controller starts workers with `dispatch` and collects their messages with
`wait`. A worker reports with `done` or asks with `ask`, then ends its turn;
the controller answers with `reply`, which wakes the worker. State lives in a
SQLite database under $WFC_STATE (default ~/.local/state/workflow-core).
"""

import argparse
import collections
import calendar
import fcntl
import hashlib
import json
import os
import re
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
    "dispatch": "id TEXT PRIMARY KEY, host TEXT, cwd TEXT, task TEXT, created REAL, scope TEXT, role TEXT, current_round INTEGER DEFAULT 1, current_attempt TEXT",
    "dispatch_round": "dispatch TEXT, round INTEGER, attempt TEXT, task TEXT, report TEXT, state TEXT DEFAULT 'running', result TEXT, created REAL, completed REAL, PRIMARY KEY (dispatch, round, attempt)",
    "message": "id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch TEXT, recipient TEXT, "
               "type TEXT, body TEXT, created REAL, delivered INTEGER DEFAULT 0, "
               "message_uid TEXT, request_key TEXT, body_hash TEXT, state TEXT DEFAULT 'unsent', "
               "status_version INTEGER DEFAULT 1",
    "screen_watch": "dispatch TEXT PRIMARY KEY, screen TEXT, since REAL, reported_screen TEXT",
    "host_error": "dispatch TEXT PRIMARY KEY, turn TEXT",
    "waiter": "pid INTEGER PRIMARY KEY, kind TEXT, since REAL",
}
# Column upgrades: `started` is set once the worker's tmux session exists (the watchdog ignores
# rows without it); `report_dir` is the directory the worker may write; `report`, `model` and
# `host_session` are what `resume` needs (Claude Code's session id is set at dispatch), and so are
# `effort` and `network`, the dispatch's reasoning effort and Codex network access, and `label`,
# its `<project> | <ticket> | <role>` name (tmux window, Claude Code session name), and `git`,
# whether a Codex worker may write its worktree's git directories.
# `process_start` tells a waiter's own process from a later one that reuses its pid.
ADDED_COLUMNS = {
    "dispatch": {"started": "REAL", "report_dir": "TEXT", "report": "TEXT", "model": "TEXT", "host_session": "TEXT",
                 "effort": "TEXT", "network": "INTEGER", "label": "TEXT", "git": "INTEGER",
                 "scope": "TEXT", "role": "TEXT", "current_round": "INTEGER", "current_attempt": "TEXT"},
    "message": {"message_uid": "TEXT", "request_key": "TEXT", "body_hash": "TEXT",
                "state": "TEXT", "status_version": "INTEGER"},
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
    # Older message rows predate the receipt ledger. Give them stable identities and a
    # conservative received/processed state based on the old delivery bit.
    for message in db.execute("SELECT id, body, message_uid, body_hash, delivered, state, status_version FROM message").fetchall():
        if message["message_uid"] and message["body_hash"] and message["state"] and message["status_version"]:
            continue
        db.execute("UPDATE message SET message_uid = ?, body_hash = ?, state = ?, status_version = ? WHERE id = ?",
                   (message["message_uid"] or "m-" + uuid.uuid4().hex,
                    message["body_hash"] or hashlib.sha256(message["body"].encode()).hexdigest(),
                    message["state"] or ("received" if message["delivered"] else "unsent"),
                    message["status_version"] or 1, message["id"]))
    db.execute("BEGIN IMMEDIATE")
    try:
        backfill_dispatch_identity(db)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return db


def backfill_dispatch_identity(db):
    """Give rows written by pre-round databases a durable identity and first snapshot."""
    rows = db.execute("SELECT id, created, task, report, report_dir, scope, role, current_round, current_attempt "
                      "FROM dispatch").fetchall()
    for row in rows:
        scope = row["scope"] or "s-" + uuid.uuid4().hex[:8]
        role = row["role"] or (clean_label(Path(row["report_dir"]).name).rpartition("-")[2]
                                if row["report_dir"] else "") or "worker"
        current_round = row["current_round"] or 1
        attempt = row["current_attempt"] or "a-" + uuid.uuid4().hex[:12]
        if (row["scope"], row["role"], row["current_round"], row["current_attempt"]) != (scope, role, current_round, attempt):
            db.execute("UPDATE dispatch SET scope = ?, role = ?, current_round = ?, current_attempt = ? WHERE id = ?",
                       (scope, role, current_round, attempt, row["id"]))
        exists = db.execute("SELECT 1 FROM dispatch_round WHERE dispatch = ?", (row["id"],)).fetchone()
        if not exists:
            db.execute("INSERT INTO dispatch_round (dispatch, round, attempt, task, report, created) VALUES (?, ?, ?, ?, ?, ?)",
                       (row["id"], current_round, attempt, row["task"], row["report"], row["created"] or time.time()))
    # A legacy dispatch may already have a done message. Preserve its complete result body.
    for snapshot in db.execute("SELECT dispatch, round, attempt, report FROM dispatch_round WHERE state = 'running'"):
        done = db.execute("SELECT body, created FROM message WHERE dispatch = ? AND type = 'done' "
                          "AND json_extract(body, '$.report') = ? ORDER BY id LIMIT 1",
                          (snapshot["dispatch"], snapshot["report"])).fetchone()
        if done:
            db.execute("UPDATE dispatch_round SET state = 'completed', result = ?, completed = ? "
                       "WHERE dispatch = ? AND round = ? AND attempt = ?",
                       (done["body"], done["created"], snapshot["dispatch"], snapshot["round"], snapshot["attempt"]))


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
            declared = {part.strip().split()[0] for part in cols.split(",")}
            added = "".join(f", {column} {kind}" for column, kind in ADDED_COLUMNS.get(table, {}).items()
                            if column not in declared)
            db.execute(f"CREATE TEMP TABLE {table} ({cols}{added})")
        elif table in missing:
            nulls = "".join(f", NULL AS {column}" for column in missing[table])
            db.execute(f"CREATE TEMP VIEW {table} AS SELECT *{nulls} FROM main.{table}")
    return db, bool(set(TABLES) - existing or missing)


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False))


FIRST_BRIEF = "You are a worker dispatched by the controller"


def worker_brief(dispatch_id, task, report, opening=FIRST_BRIEF, attempt=None):
    router = f"{shlex.quote(sys.executable)} {shlex.quote(str(PROGRAM))}"
    return (f"{opening} (dispatch {dispatch_id}). "
            f"Read and do the task in {shlex.quote(str(task))}. "
            f"Stop any server or background process you started before you report. "
            f"When the task is finished, write your report and run: "
            f"{router} done --report {shlex.quote(str(report))}"
            f"{(' --attempt ' + shlex.quote(attempt)) if attempt else ''}. "
            f"If you need a decision, run: {router} ask \"<question>\" [--options a,b]"
            f"{(' --attempt ' + shlex.quote(attempt)) if attempt else ''}, then end your turn; "
            f"the answer arrives as your next message. Never ask questions in plain text "
            f"or with an interactive question tool; nobody watches this window.")


# Each launcher returns (argv, extra environment) for an unattended worker that may write only
# its cwd plus the `writable` directories (router state and the report's directory). `conversation`
# is the host conversation to resume, or None for a new one (Claude Code: the id to give it).
# `effort` goes to the host's own reasoning-effort option; `network` opens Codex's sandbox to the
# network (the other hosts are not network-sandboxed; see IGNORED_OPTIONS); `git` adds the
# worktree's git directories to Codex's writable ones, so it can commit (the others can anyway).

def codex_command(brief, writable, cwd, model, conversation=None, resume=False, effort=None, network=False,
                  label=None, git=False):
    # --no-alt-screen keeps the transcript in the tmux scrollback, so `tmux attach` shows history.
    if git:
        writable = [*writable, *git_dirs(cwd)]
    extra = [arg for d in writable for arg in ("--add-dir", str(d))]
    models = ["-m", model] if model else []
    overrides = [*(["-c", f"model_reasoning_effort={effort}"] if effort else []),
                 *(["-c", "sandbox_workspace_write.network_access=true"] if network else [])]
    flags = ["--no-alt-screen", "-s", "workspace-write", "-a", "never", *models, *overrides, *extra]
    if resume:
        return ["codex", "resume", *flags, conversation, brief], {}
    return ["codex", *flags, brief], {}


def opencode_command(brief, writable, cwd, model, conversation=None, resume=False, effort=None, network=False,
                     label=None, git=False):
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


def claude_command(brief, writable, cwd, model, conversation=None, resume=False, effort=None, network=False,
                   label=None, git=False):
    # The brief goes first: --add-dir is variadic and would swallow a trailing prompt.
    models = ["--model", model] if model else []
    efforts = ["--effort", effort] if effort else []
    names = ["--name", label] if label else []  # shown in the prompt box, /resume picker and terminal title
    sessions = ["--resume" if resume else "--session-id", conversation] if conversation else []
    return ["claude", brief, "--permission-mode", "auto", *models, *efforts, *names, *sessions,
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
        for rollout in codex_rollouts():
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


def codex_rollouts(pattern="rollout-*.jsonl"):
    root = codex_sessions()
    return sorted(root.rglob(pattern)) if root.is_dir() else []


def codex_rollout_meta(lines):
    """The session_meta payload on a rollout's first line: `id` names the conversation."""
    return json.loads(next(lines))["payload"]


def codex_subagent(meta):
    return isinstance(meta.get("source"), dict) and "subagent" in meta["source"]


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
            meta = codex_rollout_meta(lines)
            if codex_subagent(meta):
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
IGNORED_OPTIONS = {"opencode": {"network": "--network: OpenCode is not network-sandboxed; ignored",
                                "git": "--git: OpenCode can write the git directories anyway; ignored"},
                   "claude": {"network": "--network: Claude Code is not network-sandboxed; ignored",
                              "git": "--git: Claude Code can write the git directories anyway; ignored"}}


def cmd_dispatch(args):
    db = connect()
    dispatch_id = "d-" + uuid.uuid4().hex[:8]
    scope = clean_label(args.scope or "") or "s-" + uuid.uuid4().hex[:8]
    cwd = Path(args.cwd).resolve()
    task = Path(args.task).resolve()
    report = Path(args.report).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)  # hosts grant access to existing directories
    host_session = str(uuid.uuid4()) if args.host == "claude" else None
    # A --label that cleans to nothing (only blanks or control characters) gets the default too.
    label = clean_label(args.label or "") or default_label(cwd, report)
    role = clean_label(args.role or "") or readable(report.parent.name).rpartition("-")[2] or "worker"
    attempt = "a-" + uuid.uuid4().hex[:12]
    command, extra_env = LAUNCHERS[args.host](worker_brief(dispatch_id, task, report, attempt=attempt),
                                              [state_dir(), report.parent], cwd, args.model, host_session,
                                              effort=args.effort, network=args.network, label=label, git=args.git)
    ignored = [note for option, note in IGNORED_OPTIONS.get(args.host, {}).items() if getattr(args, option)]
    trusted_by = trust_claude_worktree(cwd) if args.host == "claude" else None
    created = time.time()
    db.execute("INSERT INTO dispatch (id, host, cwd, task, created, report_dir, report, model, host_session, "
               "effort, network, label, git, scope, role, current_round, current_attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (dispatch_id, args.host, str(cwd), str(task), time.time(), str(report.parent), str(report),
                args.model, host_session, args.effort, int(args.network), label, int(args.git), scope, role, 1, attempt))
    db.execute("INSERT INTO dispatch_round (dispatch, round, attempt, task, report, created) VALUES (?, ?, ?, ?, ?, ?)",
               (dispatch_id, 1, attempt, str(task), str(report), created))
    launched = launch(dispatch_id, cwd, command, extra_env, label)
    if launched == NOT_SENT:
        db.execute("DELETE FROM dispatch_round WHERE dispatch = ?", (dispatch_id,))
        db.execute("DELETE FROM dispatch WHERE id = ?", (dispatch_id,))
        raise SystemExit("could not start the worker's tmux session; is tmux installed and on PATH?")
    db.execute("UPDATE dispatch SET started = ? WHERE id = ?", (time.time(), dispatch_id))
    if launched == UNCERTAIN:
        # Keep the record: a live worker must still be able to report. The watchdog reports
        # `exited` if no session is there.
        raise SystemExit(f"tmux did not confirm the worker's session for dispatch {dispatch_id}; it is kept. "
                         f"Check `tmux ls`; `wait` reports it exited if it never started.")
    emit({"dispatch": dispatch_id, "scope": scope, "role": role, "round": 1, "attempt": attempt,
          "host": args.host, "label": label, "tmux": session_name(dispatch_id),
          **({"trustedBy": trusted_by} if trusted_by else {}), **({"ignored": ignored} if ignored else {})})


def clean_label(text):
    """A label safe on a command line and as a tmux window name: control characters (newlines,
    tabs, escapes) become spaces, runs of white space one space."""
    return " ".join("".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text).split())[:120]


def default_label(cwd, report):
    """`<project> | <ticket> | <role>`: the project is the main repository's directory (the parent of
    the worktree's git common dir; the worktree's own name outside git), and the ticket and role
    come from the report directory, `reports/<ticket>-<role>/`, split at its last `-`."""
    try:
        common = os.fsdecode(git(cwd, "rev-parse", "--git-common-dir").rstrip(b"\n"))
        project = (Path(cwd) / common).resolve().parent.name
    except (OSError, subprocess.SubprocessError, UnicodeError):
        project = Path(cwd).name
    ticket, _, role = readable(Path(report).parent.name).rpartition("-")
    project = readable(project)
    return clean_label(" | ".join(part for part in (project, ticket, role) if part))


def git_dirs(cwd):
    """The worktree's git dir and common dir, absolute (git may print them relative to cwd), each
    once: what a sandboxed Codex worker needs writable to commit. Exits when cwd is not a repository."""
    dirs = []
    try:
        for option in ("--git-dir", "--git-common-dir"):
            path = (Path(cwd) / os.fsdecode(git(cwd, "rev-parse", option).rstrip(b"\n"))).resolve()
            dirs += [path] if path not in dirs else []
    except (OSError, subprocess.SubprocessError) as err:
        raise SystemExit(f"--git: {cwd} is not a git repository (or git failed: {err})")
    return dirs


def readable(name):
    """A file name as text that can be stored and printed: bytes that are not valid UTF-8 (kept by
    os.fsdecode as surrogates) read as U+FFFD."""
    return os.fsencode(name).decode("utf-8", "replace")


def launch(dispatch_id, cwd, command, extra_env, label=None):
    """Start the worker's tmux session. Returns SENT, NOT_SENT (nothing started) or UNCERTAIN
    (tmux ran but did not confirm: a session may exist). The label names its window (tmux then
    stops renaming it); the session keeps `wfc-<id>`, which the watchdog relies on."""
    env = {"WFC_DISPATCH": dispatch_id, "WFC_STATE": str(state_dir()), **extra_env}
    window = ["-n", label] if label else []
    started = tmux("new-session", "-d", "-s", session_name(dispatch_id), *window, "-c", str(cwd),
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


def post(db, dispatch_id, recipient, type_, body, request_key=None):
    """Append a message, or replay the existing receipt for the same request key.

    A key is the caller's stable idempotency identity. Reusing it with another payload is
    a conflict; reusing it with the same payload returns the original message id.
    """
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if request_key:
        prior = db.execute("SELECT id, body_hash FROM message WHERE dispatch = ? AND request_key = ?",
                           (dispatch_id, request_key)).fetchone()
        if prior:
            if prior["body_hash"] != digest:
                raise SystemExit(f"message request {request_key} conflicts with an existing receipt")
            return prior["id"]
    cur = db.execute("INSERT INTO message (dispatch, recipient, type, body, created, message_uid, request_key, body_hash, state, status_version) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unsent', 1)",
                     (dispatch_id, recipient, type_, encoded, time.time(), "m-" + uuid.uuid4().hex,
                      request_key, digest))
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
        return finish_round(db, dispatch_id, report, head, expected_attempt=args.attempt,
                            request_key=args.message_id)


GIT_TIMEOUT = 5


def git(cwd, *args):
    """Run git in cwd and return its output as bytes; raises OSError or SubprocessError when it
    cannot run, fails or takes longer than GIT_TIMEOUT. Bytes, because git prints file and directory
    names raw (core.quotePath=false, or any path it prints), and they need not be valid UTF-8; stderr
    is never decoded. GIT_OPTIONAL_LOCKS=0: `status` must not refresh the index, so the router never
    writes the worker's repository."""
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=GIT_TIMEOUT,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, check=True).stdout


def worktree_head(cwd):
    """{"head": commit, "dirty": bool} of the worker's worktree, for the controller to compare with
    the head its report names. Both are None when git fails, times out or cwd is not a repository."""
    try:
        head = git(cwd, "rev-parse", "--verify", "HEAD").strip().decode("ascii")  # a hex object name
        dirty = bool(git(cwd, "status", "--porcelain", "--untracked-files=normal").strip())
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return {"head": None, "dirty": None}
    return {"head": head, "dirty": dirty}


def finish_round(db, dispatch_id, report, head, expected_attempt=None, request_key=None):
    db.execute("BEGIN IMMEDIATE")
    current_row = db.execute("SELECT report, current_round, current_attempt FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()
    attempts = db.execute("SELECT COUNT(*) FROM dispatch_round WHERE dispatch = ?", (dispatch_id,)).fetchone()[0]
    same_report = db.execute("SELECT COUNT(*) FROM dispatch_round WHERE dispatch = ? AND report = ?",
                             (dispatch_id, current_row["report"])).fetchone()[0]
    if (expected_attempt and current_row["current_attempt"] != expected_attempt) or (not expected_attempt and same_report > 1):
        db.execute("ROLLBACK")
        raise SystemExit(f"attempt {expected_attempt or '(missing)'} is no longer accepted for {dispatch_id}; "
                         f"current attempt is {current_row['current_attempt']}")
    current = current_row["report"]
    # A retried `done` from an earlier round must not finish the round that followed it.
    if current and str(report) != current:
        db.execute("ROLLBACK")
        raise SystemExit(f"{report} is the report of an earlier round; the current round reports to {current}")
    body = {"report": str(report),
        "scope": db.execute("SELECT scope FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()[0],
        "role": db.execute("SELECT role FROM dispatch WHERE id = ?", (dispatch_id,)).fetchone()[0],
        "round": current_row["current_round"], "attempt": current_row["current_attempt"], **head}
    message_id = post(db, dispatch_id, "controller", "done", body, request_key=request_key)
    db.execute("UPDATE dispatch_round SET state = 'completed', result = ?, completed = ? WHERE dispatch = ? AND round = ? AND attempt = ?",
               (json.dumps({"report": str(report), **head}, ensure_ascii=False), time.time(), dispatch_id,
                current_row["current_round"], current_row["current_attempt"]))
    db.execute("COMMIT")
    message_uid = db.execute("SELECT message_uid FROM message WHERE id = ?", (message_id,)).fetchone()[0]
    emit({"message": message_id, "messageId": message_uid, "state": "unsent", "statusVersion": 1})


def cmd_ask(args):
    db = connect()
    options = [o for o in (args.options or "").split(",") if o]
    dispatch_id = current_dispatch(db)
    db.execute("BEGIN IMMEDIATE")
    identity = db.execute("SELECT scope, role, current_round, current_attempt, report FROM dispatch WHERE id = ?",
                          (dispatch_id,)).fetchone()
    same_report = db.execute("SELECT COUNT(*) FROM dispatch_round WHERE dispatch = ? AND report = ?",
                             (dispatch_id, identity["report"])).fetchone()[0]
    # Missing attempt is accepted only for a truly legacy first attempt. Once the
    # dispatch has moved to another round, an old worker's question is ambiguous.
    legacy_missing = identity["current_round"] == 1 and same_report == 1
    if (args.attempt and args.attempt != identity["current_attempt"]) or (not args.attempt and not legacy_missing):
        db.execute("ROLLBACK")
        raise SystemExit(f"attempt {args.attempt or '(missing)'} is no longer accepted for {dispatch_id}; "
                         f"current attempt is {identity['current_attempt']}")
    body = {"question": args.question, "options": options, "scope": identity["scope"],
                       "role": identity["role"], "round": identity["current_round"],
                       "attempt": identity["current_attempt"]}
    request_key = args.message_id
    message_id = post(db, dispatch_id, "controller", "question", body, request_key=request_key)
    db.execute("COMMIT")
    message_uid = db.execute("SELECT message_uid FROM message WHERE id = ?", (message_id,)).fetchone()[0]
    emit({"message": message_id, "messageId": message_uid, "state": "unsent", "statusVersion": 1,
          "next": "End your turn now; the answer arrives as your next message."})


def cmd_ack(args):
    """Mark a controller message processed. Repeating the same ACK is harmless."""
    db = connect()
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT * FROM message WHERE id = ? OR message_uid = ?", (args.message, args.message)).fetchone()
    if row is None or row["recipient"] != "controller":
        db.execute("ROLLBACK")
        raise SystemExit(f"no controller message with id {args.message}")
    if args.body is not None:
        digest = hashlib.sha256(args.body.encode()).hexdigest()
        if digest != row["body_hash"]:
            db.execute("ROLLBACK")
            raise SystemExit(f"message {args.message} ACK conflicts with its body")
    changed = row["state"] != "processed"
    if changed:
        db.execute("UPDATE message SET state = 'processed', status_version = status_version + 1 WHERE id = ?",
                   (row["id"],))
    db.execute("COMMIT")
    emit({"message": row["id"], "messageId": row["message_uid"] or str(row["id"]),
          "state": "processed", "statusVersion": (row["status_version"] or 1) + (1 if changed else 0),
          "duplicate": not changed})


SENT, NOT_SENT, UNCERTAIN, PARTIAL = "sent", "not-sent", "uncertain", "partial"
READ_ONLY = "read-only"

# What a host draws when its conversation can no longer take input. `state` texts describe the
# conversation itself: while one is on screen nothing may be typed (the Codex banner takes single
# keys such as c and n as choices), and the watchdog reports it. A `refusal` answers one Enter:
# it counts only when it is new after the router's own Enter, since an earlier one stays on screen;
# the watchdog never takes one for the session's state. Each text must start a screen
# line (after an optional `■ ` or `• `), so a worker quoting it in tool output (`│ `, `└ `) is not
# taken for the host; it may wrap over up to NOTICE_LINES lines. Claude Code and OpenCode have no
# known text yet.
READ_ONLY_NOTICES = [
    # (host, kind, text, source)
    ("codex", "state", r"This chat uses .+?, but Codex now uses .+?, so this chat is read-only\.",
     "Codex 0.167.0, a conversation resumed after the provider was switched (OpenAI to Codex API Service), "
     "under the banner `🔒  Continue in a new chat`; tmux session wfc-d-3c8ab0af, 2026-11-09"),
    ("codex", "state", r"This conversation is unavailable\. Its cached transcript and draft remain here; input is paused\.",
     "Codex 0.167.0, an older TUI whose conversation became unavailable; wfc-d-ece89726, 2026-11-09"),
    ("codex", "refusal", r"This conversation is read-only or unavailable; no operation was sent\.",
     "Codex 0.167.0, its answer to each Enter in either conversation above; wfc-d-ece89726, 2026-11-09"),
]
NOTICE_LINES = 4
_NOTICES = [(kind, re.compile(text)) for _, kind, text, _ in READ_ONLY_NOTICES]
_NOTICE_MARK = re.compile(r"(?:[■•]\s*)?")


def screen_notices(screen):
    """(kind, text) of every read-only notice on a screen, top to bottom, each as one line."""
    lines = [line.strip() for line in screen.splitlines()]
    found = []
    for i, line in enumerate(lines):
        if not line:
            continue
        joined = " ".join(" ".join(lines[i:i + NOTICE_LINES]).split())
        joined = joined[_NOTICE_MARK.match(joined).end():]
        for kind, pattern in _NOTICES:
            matched = pattern.match(joined)
            if matched:
                found.append((kind, matched.group(0)))
                break
    return found


def read_only_notice(screen):
    """The screen's first state notice: what makes it evidence that the session is read-only now.
    A refusal only answers one Enter; one left on screen says nothing about the session now."""
    return next((text for kind, text in screen_notices(screen) if kind == "state"), None)


def refusals(found):
    return [text for kind, text in found if kind == "refusal"]


def capture(session):
    shot = tmux("capture-pane", "-p", "-t", session)
    return shot.stdout if shot.returncode == 0 else None


def seconds_setting(name, default):
    try:
        return max(0.0, float(os.environ.get(name, default)))
    except ValueError:
        return default


def new_notice(session, before, seconds):
    """Watch the screen for `seconds` for evidence that the session takes no input: any state
    notice, or more refusals than `before` (the notices on screen before the Enter) had, which
    answer this Enter. Returns that line, or None."""
    deadline = time.monotonic() + seconds
    earlier = len(refusals(before))
    while True:
        time.sleep(max(0.0, min(0.25, deadline - time.monotonic())))
        screen = capture(session)
        if screen is not None:
            found = screen_notices(screen)
            state = next((text for kind, text in found if kind == "state"), None)
            if state:
                return state
            if len(refusals(found)) > earlier:
                return refusals(found)[-1]
        if time.monotonic() >= deadline:
            return None


# Where a host's input box is on its screen. A long text can still be arriving when its Enter
# does, and the TUI then drops the Enter: the text stays in the box, unsent (#18). Only the box
# tells: Codex repeats each submitted message in the transcript after the same `›`. Claude Code and
# OpenCode have no reader yet (see the README), so they get one Enter, as before.
ENTER_RETRIES = 2
_BOX_SCROLL = re.compile(r"\s*[↑↓]$")
# What a reader saw in an input box: its text, and whether lines above it are scrolled out of
# sight (Codex marks that with ↑), so that the text may start inside the typed text.
InputBox = collections.namedtuple("InputBox", "text scrolled")
EMPTY_BOX = InputBox("", False)
# What an empty input box shows. A box showing one of these is empty: it is never compared with the
# typed text, which may contain it (review R6-1).
INPUT_BOX_PLACEHOLDERS = [
    # (host, text, source)
    ("codex", "Ask Codex to do anything",
     "Codex 0.167.0, three fresh starts on a private tmux socket, 2026-11-10, drawn dim (SGR 2) unlike typed "
     "text (docs/probes-20261110-input-box.md); also on the 2026-11-09 screens of wfc-d-ece89726"),
]
_PLACEHOLDERS = {}
for _host, _text, _ in INPUT_BOX_PLACEHOLDERS:
    _PLACEHOLDERS.setdefault(_host, set()).add("".join(_text.split()))


def codex_input_box(screen):
    """What Codex's input box holds (an InputBox): EMPTY_BOX when it shows a known placeholder, None
    when the screen shows no box we can tell apart.
    Codex 0.167.0 draws the box as the last paragraph that starts with `›` at the left edge, its
    further lines indented by two spaces, then (usually after a blank line) a footer of one or two
    indented lines joined by ` · ` (model, cwd; shortcuts). A tall box scrolls and shows ↑ or ↓ at
    its right edge. A message already submitted sits higher up in the transcript, after a `›` too,
    but never directly above the footer: the box is always drawn below it."""
    lines = [line.rstrip() for line in screen.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    starts = [i for i, line in enumerate(lines) if line == "›" or line.startswith("› ")]
    if not starts:
        return None
    end = starts[-1] + 1
    while end < len(lines) and lines[end].startswith("  ") and lines[end].strip():
        end += 1
    box, below = lines[starts[-1]:end], lines[end:]
    if not below:  # no blank line above the footer (message 176's screen): it ended the paragraph
        while len(box) > 1 and len(below) < 2 and " · " in box[-1]:
            below.insert(0, box.pop())
    footer = [line for line in below if line]
    if not 1 <= len(footer) <= 2 or not all(line.startswith("  ") and " · " in line for line in footer):
        return None
    box[0] = box[0][1:]
    text = " ".join(_BOX_SCROLL.sub("", line).strip() for line in box)
    if "".join(text.split()) in _PLACEHOLDERS["codex"]:
        return EMPTY_BOX
    return InputBox(text, any(line.endswith("↑") for line in box))


INPUT_BOXES = {"codex": codex_input_box}


def holds_typed_text(box, text):
    """The box shows the start of `text` (or, scrolled, a later part of it), ignoring how it was
    wrapped. An empty box (EMPTY_BOX: the reader saw a known placeholder) or text the router did not
    type does not count. A placeholder the reader does not know reads as text (`capture-pane -p`
    drops the dim style Codex draws it in), but it is never scrolled and does not start the typed
    text, which starts with `[router]`, so it gets no Enter either: when the screen cannot tell an
    empty box from the text, nothing is pressed."""
    shown, typed = "".join(box.text.split()), "".join(text.split())
    if not shown or len(shown) < min(len(typed), 8):
        return False
    return shown in typed if box.scrolled else typed.startswith(shown)


def text_left_in_box(session, host, text):
    """The screen when the host's input box still holds `text` and stays the same over two
    captures 0.3 s apart, so it is not still being drawn or emptied; else None. Hosts without a
    known box, and screens that cannot be captured or read, give None: nothing more is pressed."""
    reader = INPUT_BOXES.get(host)
    if reader is None:
        return None
    first = capture(session)
    if first is None:
        return None
    time.sleep(0.3)
    second = capture(session)
    if second is None:
        return None
    box = reader(second)
    if box is None or box != reader(first) or not holds_typed_text(box, text):
        return None
    return second


def type_into(session, text, host=None):
    """Type one line into a TUI and submit it. Returns (state, notice), the state being
    SENT;
    NOT_SENT when the text certainly did not arrive (tmux could not be run at all): safe to type again;
    READ_ONLY when the host's screen says its conversation takes no input (`notice` is that line):
      either it said so before anything was typed, or it answered an Enter with a new refusal.
      Either way nothing was sent, and nothing typed later will be;
    UNCERTAIN when the text command ran but did not confirm (it may have handed the text over
      before failing, and a follow-up probe could fail the same way, so none is trusted);
    PARTIAL when the text arrived but its Enter was not confirmed: tmux failed to press it, or
      the text was still in the host's input box after ENTER_RETRIES more Enters.
    After UNCERTAIN or PARTIAL, typing again could double the line: someone has to look.
    A SENT costs the check window after the Enter (WFC_TYPED_CHECK_SECONDS, 1.5 s by default).
    For a host with a known input box (INPUT_BOXES), a dropped Enter is pressed again, at most
    ENTER_RETRIES times, each only while the box still holds this text and stays unchanged: a box
    that is empty, shows other text, or is still changing gets no Enter, so nothing is submitted
    twice and no empty Enter is sent. Each extra Enter gets its own check window."""
    screen = capture(session)
    before = screen_notices(screen) if screen is not None else None
    if before and any(kind == "state" for kind, _ in before):
        return READ_ONLY, next(t for kind, t in before if kind == "state")
    sent = tmux("send-keys", "-t", session, "-l", text)
    if sent.returncode != 0:
        return (NOT_SENT if getattr(sent, "not_run", False) else UNCERTAIN), None
    time.sleep(0.5)  # TUIs drop an Enter that arrives in the same burst as the text
    if tmux("send-keys", "-t", session, "Enter").returncode != 0:
        return PARTIAL, None
    if before is None:  # no screen to compare with: tmux could not capture it
        return SENT, None
    window = seconds_setting("WFC_TYPED_CHECK_SECONDS", 1.5)
    notice = new_notice(session, before, window)
    if notice:
        return READ_ONLY, notice
    for _ in range(ENTER_RETRIES):
        left = text_left_in_box(session, host, text)
        if left is None:
            return SENT, None
        if tmux("send-keys", "-t", session, "Enter").returncode != 0:
            return PARTIAL, None
        notice = new_notice(session, screen_notices(left), window)
        if notice:
            return READ_ONLY, notice
    return (PARTIAL, None) if text_left_in_box(session, host, text) is not None else (SENT, None)


def refusal(session, what, notice):
    return (f"tmux session {session} did not take {what}: its screen says \"{notice}\" Nothing was sent, and this "
            f"conversation cannot go on; dispatch a new worker")


def mark_screen_reported(db, dispatch_id, session):
    """The command that met a read-only screen reports it, so the watchdog does not report it again."""
    screen = capture(session)
    if screen is not None:
        db.execute("INSERT OR REPLACE INTO screen_watch (dispatch, screen, since, reported_screen) VALUES (?, ?, ?, ?)",
                   (dispatch_id, screen, time.time(), screen))


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
    host = db.execute("SELECT host FROM dispatch WHERE id = ?", (question["dispatch"],)).fetchone()
    with input_lock(question["dispatch"]):
        typed, notice = type_into(session, f"[router] Answer to question {question['id']}: {answer}",
                                  host["host"] if host else None)
    if typed in (NOT_SENT, READ_ONLY):
        db.execute("DELETE FROM message WHERE id = ?", (reply_id,))
        if typed == READ_ONLY:
            mark_screen_reported(db, question["dispatch"], session)
            raise SystemExit(refusal(session, "the answer", notice))
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
    previous = db.execute("SELECT task, report, current_round, current_attempt FROM dispatch WHERE id = ?", (args.dispatch,)).fetchone()
    identity = db.execute("SELECT scope, role, current_round FROM dispatch WHERE id = ?", (args.dispatch,)).fetchone()
    next_round, attempt = identity["current_round"] + 1, "a-" + uuid.uuid4().hex[:12]
    message_id = post(db, args.dispatch, "worker", "continue", {"task": str(task), "report": str(report),
        "scope": identity["scope"], "role": identity["role"], "round": next_round, "attempt": attempt})
    db.execute("INSERT INTO dispatch_round (dispatch, round, attempt, task, report, created) VALUES (?, ?, ?, ?, ?, ?)",
               (args.dispatch, next_round, attempt, str(task), str(report), time.time()))
    db.execute("UPDATE dispatch SET task = ?, report = ?, current_round = ?, current_attempt = ? WHERE id = ?",
               (str(task), str(report), next_round, attempt, args.dispatch))
    db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (args.dispatch,))
    db.execute("COMMIT")
    brief = worker_brief(args.dispatch, task, report, opening="[router] New task for you", attempt=attempt)
    typed, notice = type_into(session, brief, row["host"])
    if typed in (NOT_SENT, READ_ONLY):
        why = (refusal(session, "the next task", notice) if typed == READ_ONLY
               else f"could not type the next task into tmux session {session}")
        # Undo the claim only while it is still the latest round event and its task is still the
        # current one: a `resume` may have taken the new round over in between.
        db.execute("BEGIN IMMEDIATE")
        if typed == READ_ONLY:
            mark_screen_reported(db, args.dispatch, session)
        current = db.execute("SELECT task, report FROM dispatch WHERE id = ?", (args.dispatch,)).fetchone()
        if round_mark(db, args.dispatch) == message_id and (current["task"], current["report"]) == (str(task), str(report)):
            db.execute("DELETE FROM message WHERE id = ?", (message_id,))
            db.execute("DELETE FROM dispatch_round WHERE dispatch = ? AND round = ? AND attempt = ?",
                       (args.dispatch, next_round, attempt))
            db.execute("UPDATE dispatch SET task = ?, report = ?, current_round = ?, current_attempt = ? WHERE id = ?",
                       (previous["task"], previous["report"], previous["current_round"], previous["current_attempt"], args.dispatch))
            db.execute("COMMIT")
            raise SystemExit(why)
        db.execute("COMMIT")
        raise SystemExit(f"{why}. But dispatch {args.dispatch} moved on meanwhile (a resume took the round over); "
                         f"its current state is kept")
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
    typed, notice = type_into(session, f"[router] Message from the controller: {text}", row["host"])
    if typed in (NOT_SENT, READ_ONLY):
        db.execute("DELETE FROM message WHERE id = ?", (message_id,))
        if typed == READ_ONLY:
            mark_screen_reported(db, args.dispatch, session)
            raise SystemExit(refusal(session, "the message", notice))
        raise SystemExit(f"could not type the message into tmux session {session}")
    if typed in (PARTIAL, UNCERTAIN):
        raise typing_error(session, "The message", typed)
    emit({"nudged": args.dispatch, "message": message_id})


def local_time(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else None


def message_view(r):
    return {"id": r["id"], "messageId": r["message_uid"] or str(r["id"]),
            "dispatch": r["dispatch"], "type": r["type"], "to": r["recipient"], "at": local_time(r["created"]),
            "state": r["state"] or ("received" if r["delivered"] else "unsent"),
            "statusVersion": r["status_version"] or 1,
            **({"delivered": bool(r["delivered"])} if r["recipient"] == "controller" else {}), **json.loads(r["body"])}


def dispatch_state(db, dispatch_id):
    if done_current_task(db, dispatch_id):
        return "done"
    if reclaimed(db, dispatch_id):
        return "reclaimed"
    if not watched(db, dispatch_id):
        return "exited"
    return "asking" if awaiting_answer(db, dispatch_id) else "working"


def cmd_status(args):
    """Read-only view of the scope: every dispatch, undelivered messages and the running waiters.
    With --active, only the unfinished dispatches, plus the finished ones whose tmux session is still
    alive under `reclaimable`. It never creates, upgrades or changes router.db; write commands do that."""
    db, outdated = connect_read_only()
    if db is None:
        if args.dispatch:
            raise SystemExit(f"unknown dispatch {args.dispatch} in {state_dir()}")
        emit({"state": str(state_dir()), "dispatches": [], **({"reclaimable": []} if args.active else {}),
              "undelivered": [], "waiters": []})
        return
    query = "SELECT * FROM dispatch WHERE started IS NOT NULL" + (" AND id = ?" if args.dispatch else "") + " ORDER BY created"
    rows = db.execute(query, (args.dispatch,) if args.dispatch else ()).fetchall()
    if args.dispatch and not rows:
        raise SystemExit(f"unknown dispatch {args.dispatch} in {state_dir()}")
    now, dispatches, reclaimable = time.time(), [], []
    for row in rows:
        state = dispatch_state(db, row["id"])
        closed = reclaimed(db, row["id"])
        if args.active and state == "done":
            if not closed and session_alive(session_name(row["id"])):
                reclaimable.append({"dispatch": row["id"], "label": row["label"], "tmux": session_name(row["id"])})
            continue
        last = db.execute("SELECT * FROM message WHERE dispatch = ? ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        watch = db.execute("SELECT since FROM screen_watch WHERE dispatch = ?", (row["id"],)).fetchone()
        rounds = [dict(r) for r in db.execute("SELECT round, attempt, task, report, state, result, created, completed "
                                               "FROM dispatch_round WHERE dispatch = ? ORDER BY round, created", (row["id"],))]
        view = {"dispatch": row["id"], "scope": row["scope"], "role": row["role"],
                "round": row["current_round"], "attempt": row["current_attempt"], "rounds": rounds,
                "label": row["label"], "host": row["host"], "model": row["model"],
                "git": bool(row["git"]), "state": state,
                "tmux": session_name(row["id"]), "tmuxAlive": session_alive(session_name(row["id"])),
                "cwd": row["cwd"], "task": row["task"], "report": row["report"], "started": local_time(row["started"]),
                "last": message_view(last) if last else None,
                **({"screenIdleSeconds": int(now - watch["since"])} if watch else {}),
                **({"reclaimed": local_time(closed)} if closed else {})}
        if args.dispatch:
            view["messages"] = [message_view(m) for m in
                                db.execute("SELECT * FROM message WHERE dispatch = ? ORDER BY id", (row["id"],))]
        dispatches.append(view)
    undelivered = db.execute("SELECT * FROM message WHERE recipient = 'controller' AND delivered = 0 "
                             + ("AND dispatch = ? " if args.dispatch else "") + "ORDER BY id",
                             (args.dispatch,) if args.dispatch else ()).fetchall()
    waiters = [{"pid": w["pid"], "kind": w["kind"], "since": local_time(w["since"])}
               for w in db.execute("SELECT * FROM waiter ORDER BY since") if waiter_alive(w)]
    emit({"state": str(state_dir()), "dispatches": dispatches, **({"reclaimable": reclaimable} if args.active else {}),
          "undelivered": [message_view(m) for m in undelivered], "waiters": waiters,
          **({"schema": "router.db is older than this wfc and is shown as far as it goes; "
                        "the next write command (wait, dispatch, ...) upgrades it"} if outdated else {})})


# An lstart is the boot time plus the start offset, so it moves when the clock is corrected: a relay
# registered at 17:15:10 read back as 17:15:07 (#18). How far an lstart may move and still name
# the same process; a pid reused within that is taken for the old process.
LSTART_SLACK = 10


def waiter_alive(row):
    """A row registered with its process start names that one process; an older row has only its pid.
    A Linux start (`linux:<boot id>:<starttime>`) must match exactly; an lstart, registered elsewhere
    or by wfc up to 0.4.4, within LSTART_SLACK seconds."""
    registered = row["process_start"]
    if not registered:
        return process_alive(row["pid"])
    if registered.startswith("linux:"):
        return process_start(row["pid"]) == registered
    now = process_lstart(row["pid"])
    if now is None:
        return False
    then_epoch, now_epoch = lstart_epoch(registered), lstart_epoch(now)
    if then_epoch is None or now_epoch is None:
        return now == registered
    return abs(now_epoch - then_epoch) <= LSTART_SLACK


def process_start(pid):
    """What names process `pid` among all that ever had its pid, or None when there is no such process.
    On Linux `linux:<boot id>:<starttime>`, the starttime being field 22 of /proc/<pid>/stat (clock
    ticks after boot), which no clock correction moves; elsewhere its lstart. The boot id tells a
    process from one of an earlier boot with the same pid and starttime. When it cannot be read the
    part is left empty, and such a row only names a pid and starttime: across a reboot it may be taken
    for a later process with both the same."""
    if not Path("/proc/self/stat").exists():
        return process_lstart(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot = ""
    # Field 2 (the command) is in parentheses and may hold spaces; field 3 follows the last `)`.
    return f"linux:{boot}:{stat[stat.rindex(')') + 2:].split()[19]}"


def process_lstart(pid):
    """When process `pid` started, as `ps` prints it in UTC, or None when there is no such process."""
    try:
        result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except OSError:
        return None
    return result.stdout.strip() or None


def lstart_epoch(text):
    try:
        return calendar.timegm(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


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
    resume_round = row["current_round"] + (1 if args.task else 0)
    resume_attempt = "a-" + uuid.uuid4().hex[:12]
    host_session = find_host_session(row)
    if not host_session:
        raise SystemExit(f"cannot find the {row['host']} conversation of dispatch {args.dispatch}; dispatch a new worker")
    opening = ("[router] Your session was interrupted and has been resumed. Check the worktree and what you "
               "already did, then continue" if not args.task else "[router] Resumed with a new task for you")
    command, extra_env = LAUNCHERS[row["host"]](worker_brief(args.dispatch, task, report, opening=opening,
                                                              attempt=resume_attempt),
                                                [state_dir(), Path(row["report_dir"])], Path(row["cwd"]),
                                                row["model"], host_session, resume=True,
                                                effort=row["effort"], network=bool(row["network"]), label=row["label"],
                                                git=bool(row["git"]))
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
        identity = db.execute("SELECT scope, role, current_round, current_attempt FROM dispatch WHERE id = ?",
                              (args.dispatch,)).fetchone()
        # Every resume is a new execution attempt and snapshot.  Keeping the same task/report
        # for an interrupted worker still preserves the prior attempt for diagnosis.
        next_round = identity["current_round"] + (1 if args.task else 0)
        attempt = resume_attempt
        if not args.task:
            db.execute("UPDATE dispatch_round SET state = 'unknown', completed = ? WHERE dispatch = ? AND round = ? AND attempt = ?",
                       (time.time(), args.dispatch, identity["current_round"], identity["current_attempt"]))
        post(db, args.dispatch, "worker", "resumed", {"session": host_session, "scope": identity["scope"],
             "role": identity["role"], "round": next_round, "attempt": attempt})
        if args.task:
            post(db, args.dispatch, "worker", "continue", {"task": str(task), "report": str(report),
                 "scope": identity["scope"], "role": identity["role"], "round": next_round, "attempt": attempt})
        db.execute("INSERT INTO dispatch_round (dispatch, round, attempt, task, report, created) VALUES (?, ?, ?, ?, ?, ?)",
                   (args.dispatch, next_round, attempt, str(task), str(report), time.time()))
        db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (args.dispatch,))
        launched = launch(args.dispatch, row["cwd"], command, extra_env, row["label"])
        if launched == NOT_SENT:
            db.execute("ROLLBACK")
            raise SystemExit(f"could not start tmux session {session}")
        # The session (possibly) runs this task: record it with the launch, before typing can fail.
        db.execute("UPDATE dispatch SET task = ?, report = ?, host_session = ?, current_round = ?, current_attempt = ? WHERE id = ?",
                   (str(task), str(report), host_session, next_round, attempt, args.dispatch))
        db.execute("COMMIT")
        if launched == UNCERTAIN:
            raise SystemExit(f"tmux did not confirm {session}; the resumed round is recorded. Check `tmux ls`; "
                             f"`wait` reports it exited if it never started.")
        if row["host"] == "opencode":
            brief = worker_brief(args.dispatch, task, report, opening=opening, attempt=attempt)
            typed, notice = type_into(session, brief, row["host"]) if wait_for_quiet_screen(session) else (NOT_SENT, None)
            if typed in (PARTIAL, UNCERTAIN):
                raise typing_error(session, "The brief", typed)
            if typed == READ_ONLY:
                mark_screen_reported(db, args.dispatch, session)
                raise SystemExit(f"resumed {session}, but " + refusal(session, "the brief", notice))
            if typed == NOT_SENT:
                raise SystemExit(f"resumed {session} but could not type the brief into it; attach and paste it: {brief}")
        elif any(host == row["host"] for host, *_ in READ_ONLY_NOTICES):
            # The brief went in on the command line; a conversation that opens read-only drops it.
            notice = new_notice(session, [], seconds_setting("WFC_RESUME_CHECK_SECONDS", 5))
            if notice:
                mark_screen_reported(db, args.dispatch, session)
                raise SystemExit(f"resumed {session}, but its screen says \"{notice}\" The brief was not sent, and "
                                 f"this conversation cannot go on; dispatch a new worker (the resumed round stays "
                                 f"recorded; close the session with tmux kill-session -t {session})")
    emit({"resumed": args.dispatch, "session": host_session, "tmux": session,
          "round": next_round, "attempt": attempt})


def session_owner(session):
    """(True, None) when the tmux session's WFC_STATE is this state directory, else (False, why)."""
    shown = tmux("show-environment", "-t", session, "WFC_STATE")
    if shown.returncode != 0:
        return False, f"cannot read WFC_STATE of tmux session {session}: {(shown.stderr or '').strip() or 'not set'}"
    value = shown.stdout.strip().partition("=")[2]
    if not value or Path(value).expanduser().resolve() != state_dir():
        return False, f"tmux session {session} belongs to state directory {value or '(none)'}, not {state_dir()}"
    return True, None


def reclaim_targets(db, named):
    """The dispatches reclaim considers: the named ones (each must exist), or else every done one
    whose session is alive and not yet reclaimed, as `status --active` lists under `reclaimable`."""
    rows = []
    for dispatch_id in named:
        row = db.execute("SELECT * FROM dispatch WHERE id = ? AND started IS NOT NULL", (dispatch_id,)).fetchone()
        if row is None:
            raise SystemExit(f"unknown dispatch {dispatch_id} in {state_dir()}")
        rows.append(row)
    if named:
        return rows
    return [row for row in db.execute("SELECT * FROM dispatch WHERE started IS NOT NULL ORDER BY created")
            if done_current_task(db, row["id"]) and not reclaimed(db, row["id"])
            and session_alive(session_name(row["id"]))]


def cmd_reclaim(args):
    """List the tmux sessions of finished dispatches; with --yes, close them. An unfinished dispatch
    is listed and closed only when named. Only `wfc-<id>` sessions of this state directory are
    closed; router.db keeps every record (resume and the ledger need them), and worktrees,
    branches and host conversations are left alone. Without --yes nothing is changed."""
    named = list(dict.fromkeys(args.dispatch or []))
    if args.yes:
        db = connect()
    else:
        db, _ = connect_read_only()
        if db is None:
            if named:
                raise SystemExit(f"unknown dispatch {named[0]} in {state_dir()}")
            emit({"state": str(state_dir()), "yes": False, "sessions": []})
            return 0
    sessions, errors = [], []
    for row in reclaim_targets(db, named):
        dispatch_id, session = row["id"], session_name(row["id"])
        state = dispatch_state(db, dispatch_id)
        item = {"dispatch": dispatch_id, "label": row["label"], "tmux": session, "state": state,
                "reclaimable": state == "done" and not reclaimed(db, dispatch_id)}
        if not item["reclaimable"]:
            item["note"] = (f"not in reclaimable ({'already reclaimed' if reclaimed(db, dispatch_id) else 'its task is not done'}); "
                            f"closed only when named and with --yes")
        sessions.append(item)
        if not session_alive(session):
            item.update(result="gone", note=f"tmux session {session} no longer exists; skipped")
            continue
        owned, why = session_owner(session)
        if not owned:
            item.update(result="refused", error=why)
            if args.yes:  # a listing only shows it: nothing was attempted
                errors.append(why)
            continue
        if not args.yes:
            item["result"] = "listed"
            continue
        item.update(reclaim_session(db, row, named=bool(named)))
        if item["result"] == "failed":
            errors.append(item["error"])
    emit({"state": str(state_dir()), "yes": bool(args.yes), "sessions": sessions})
    for error in errors:
        print(f"router: {error}", file=sys.stderr)
    return 1 if errors else 0


def reclaim_session(db, row, named):
    """Close one session and record `reclaimed`, under the input lock and the write lock the
    watchdog takes, so it never sees the session gone without the record. A dispatch reclaim chose
    by itself (not `named` with --dispatch) must still be done and not reclaimed under the lock: one
    a `continue` or `resume` moved on since it was listed is left running (`changed`). A named one
    is closed whatever its task's state, as naming it is the user's explicit decision."""
    dispatch_id, session = row["id"], session_name(row["id"])
    with input_lock(dispatch_id):
        db.execute("BEGIN IMMEDIATE")
        try:
            if not named and (not done_current_task(db, dispatch_id) or reclaimed(db, dispatch_id)):
                db.execute("ROLLBACK")
                return {"result": "changed", "note": f"dispatch {dispatch_id} is no longer done and unreclaimed "
                                                     f"(a continue, resume or reclaim came meanwhile); left running"}
            if not session_alive(session):
                db.execute("ROLLBACK")
                return {"result": "gone", "note": f"tmux session {session} no longer exists; skipped"}
            owned, why = session_owner(session)
            if not owned:
                db.execute("ROLLBACK")
                return {"result": "failed", "error": why}
            killed = tmux("kill-session", "-t", session)
            if killed.returncode != 0:
                db.execute("ROLLBACK")
                return {"result": "failed", "error": f"could not close tmux session {session} "
                                                     f"(exit {killed.returncode}): {(killed.stderr or '').strip()}"}
            message_id = post(db, dispatch_id, "worker", "reclaimed", {"tmux": session})
            db.execute("DELETE FROM screen_watch WHERE dispatch = ?", (dispatch_id,))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"result": "reclaimed", "message": message_id}


def take_controller_messages(db):
    db.execute("BEGIN IMMEDIATE")
    rows = db.execute("SELECT * FROM message WHERE recipient = 'controller' AND delivered = 0 ORDER BY id").fetchall()
    db.executemany("UPDATE message SET delivered = 1, state = 'received', status_version = status_version + 1 WHERE id = ?",
                   [(r["id"],) for r in rows])
    db.execute("COMMIT")
    return [{"id": r["id"], "messageId": r["message_uid"] or str(r["id"]), "dispatch": r["dispatch"],
             "type": r["type"], "state": "received", "statusVersion": (r["status_version"] or 1) + 1,
             **json.loads(r["body"])} for r in rows]


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


def reclaimed(db, dispatch_id):
    """When `reclaim` closed the dispatch's session, if no `resume` reopened it since; else None.
    A reclaimed session is not watched. `reclaimed` is not a round event: it changes neither the
    round mark nor whether the task is done, so a later `resume` works as before."""
    row = db.execute(
        "SELECT m.created FROM message m WHERE m.dispatch = ? AND m.type = 'reclaimed' AND m.id > "
        "COALESCE((SELECT MAX(r.id) FROM message r WHERE r.dispatch = m.dispatch AND r.type = 'resumed'), 0) "
        "ORDER BY m.id DESC LIMIT 1", (dispatch_id,)).fetchone()
    return row[0] if row else None


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
        if not watched(db, dispatch_id) or reclaimed(db, dispatch_id):
            continue
        session = session_name(dispatch_id)
        alive = session_alive(session)
        captured = tmux("capture-pane", "-p", "-t", session) if alive else None
        now = time.time()
        # A resume starts a fresh watch: the host may record the reopened conversation elsewhere.
        watch_key = (dispatch_id, db.execute("SELECT MAX(id) FROM message WHERE dispatch = ? AND type = 'resumed'",
                                             (dispatch_id,)).fetchone()[0])
        turn_end = TURN_WATCHES.setdefault(watch_key, TurnWatch()).latest(row, now) if alive else None
        notice = read_only_notice(captured.stdout) if alive and captured.returncode == 0 else None
        # Decide and post under one write lock, re-checking state a worker's `done` may have changed.
        db.execute("BEGIN IMMEDIATE")
        try:
            if not watched(db, dispatch_id) or reclaimed(db, dispatch_id):
                pass
            elif not alive and session_alive(session):
                pass  # a stale sample: `resume` relaunched it while we sampled; look again next pass
            elif not alive:
                post(db, dispatch_id, "controller", "exited",
                     {"reason": f"tmux session {session} ended without done"})
            elif notice:
                # Nothing typed into this session will arrive: report it now, once per screen.
                watch = db.execute("SELECT reported_screen FROM screen_watch WHERE dispatch = ?",
                                   (dispatch_id,)).fetchone()
                if watch is None or watch["reported_screen"] != captured.stdout:
                    post(db, dispatch_id, "controller", "blocked",
                         {"host": row["host"], "kind": "session_read_only", "message": notice})
                    db.execute("INSERT OR REPLACE INTO screen_watch (dispatch, screen, since, reported_screen) "
                               "VALUES (?, ?, ?, ?)", (dispatch_id, captured.stdout, now, captured.stdout))
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
    Returns "delivered", "failed" (codex could not even start: safe to retry), "missing" (codex
    found no rollout for the thread: certainly not delivered, and retrying cannot help) or
    "uncertain" (codex ran but did not confirm, e.g. a timeout or a non-zero exit that may follow
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
    if result.returncode != 0 and "no rollout found for thread" in result.stdout + result.stderr:
        print(f"router relay: codex queue found no conversation for thread {thread}: "
              f"{(result.stderr or result.stdout).strip()}; messages {ids} stay waiting. Relay stopped.",
              file=sys.stderr, flush=True)
        return "missing"
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


def checked_codex_thread(thread):
    """`thread` as a lower-case UUID, once a top-level Codex rollout names it; exits otherwise.
    `codex queue` accepts any thread, so a wrong id would deliver nothing and still look delivered.
    Codex names each rollout `rollout-<time>-<id>.jsonl`, so only that file is opened."""
    try:
        thread = str(uuid.UUID(thread))
    except ValueError:
        raise SystemExit(f"--codex-thread {thread!r} is not a session UUID; give the controller's UUID "
                         f"(a thread name cannot be checked) or run `relay --codex` inside the controller")
    for rollout in codex_rollouts(f"rollout-*-{thread}.jsonl"):
        try:
            with open(rollout, errors="replace") as lines:
                meta = codex_rollout_meta(lines)
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            continue
        if not isinstance(meta, dict) or meta.get("id") != thread:
            continue
        if codex_subagent(meta):
            raise SystemExit(f"Codex thread {thread} is a sub-agent ({rollout}); relay to the controller's "
                             f"own thread")
        current = os.environ.get("CODEX_THREAD_ID")
        if current and current.lower() != thread:
            raise SystemExit(f"--codex-thread {thread} is not this Codex session's thread (CODEX_THREAD_ID="
                             f"{current}); use `relay --codex`, or unset CODEX_THREAD_ID to relay into another "
                             f"session")
        return thread
    raise SystemExit(f"no rollout for Codex thread {thread} under {codex_sessions()}; check the id "
                     f"(codex queue silently accepts a thread that does not exist)")


def relay_target(args):
    """The controller to push into: a Codex thread, or an OpenCode session at a URL."""
    opencode = bool(args.opencode_url) or bool(args.opencode_session)
    codex = args.codex or bool(args.codex_thread)
    if args.codex and args.codex_thread:
        raise SystemExit("give --codex or --codex-thread, not both")
    if codex == opencode or (opencode and not (args.opencode_url and args.opencode_session)):
        raise SystemExit("give either --codex, --codex-thread, or both --opencode-url and --opencode-session")
    if opencode and not args.opencode_url.startswith(("http://", "https://")):
        raise SystemExit("--opencode-url must be an http(s) URL, e.g. http://127.0.0.1:4096")
    if codex:
        if args.codex and not os.environ.get("CODEX_THREAD_ID"):
            raise SystemExit("--codex needs CODEX_THREAD_ID, which Codex sets for its shell commands; "
                             "outside Codex give --codex-thread <session-uuid>")
        thread = checked_codex_thread(args.codex_thread or os.environ["CODEX_THREAD_ID"])
        return ["--codex-thread", thread], lambda batch: queue_into_codex(thread, batch)
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
            outcome = deliver(messages) if messages else None
            if outcome in ("failed", "missing"):
                # Put the batch back so the next pass, or a `wait`, delivers it.
                db.executemany("UPDATE message SET delivered = 0 WHERE id = ?", [(m["id"],) for m in messages])
                if outcome == "missing":
                    return 1  # the thread is gone or wrong: every later batch would be lost too
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
    p.add_argument("--scope", help="stable controller scope identity (generated when omitted)")
    p.add_argument("--role", help="stable worker role identity (derived from the report directory when omitted)")
    p.add_argument("--model", help="model for the worker; passed to the host's own model flag")
    p.add_argument("--effort", help="reasoning effort, passed to the host's own option "
                                    "(Codex model_reasoning_effort, Claude Code --effort, OpenCode --variant)")
    p.add_argument("--label", help="the dispatch's name, shown in status, as its tmux window name and as the "
                                   "Claude Code session name (default: <project> | <ticket> | <role>)")
    p.add_argument("--git", action="store_true",
                   help="let a Codex worker commit: add the worktree's git dir and common dir to its writable "
                        "dirs (it can then change anything under the main repository's .git)")
    p.add_argument("--network", action="store_true",
                   help="let a Codex worker's sandbox reach the network (other hosts are not sandboxed)")
    p.set_defaults(func=cmd_dispatch)
    p = sub.add_parser("done")
    p.add_argument("--report", required=True)
    p.add_argument("--attempt", help="execution attempt identity from the worker brief")
    p.add_argument("--message-id", help="stable worker receipt key; retries are idempotent")
    p.set_defaults(func=cmd_done)
    p = sub.add_parser("ask")
    p.add_argument("question")
    p.add_argument("--options")
    p.add_argument("--attempt", help="execution attempt identity from the worker brief")
    p.add_argument("--message-id", help="stable worker receipt key; retries are idempotent")
    p.set_defaults(func=cmd_ask)
    p = sub.add_parser("ack", help="mark a controller message processed")
    p.add_argument("message", help="numeric message id or stable message id")
    p.add_argument("--body", help="canonical receipt body, when available")
    p.set_defaults(func=cmd_ack)
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
    one = p.add_mutually_exclusive_group()
    one.add_argument("--dispatch", help="one dispatch, with its whole message history")
    one.add_argument("--active", action="store_true",
                     help="only unfinished dispatches (working, asking, exited); finished ones whose tmux session "
                          "is still alive are listed under reclaimable")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("reclaim", help="list the tmux sessions of finished dispatches; close them with --yes")
    p.add_argument("--dispatch", action="append",
                   help="only this dispatch (repeatable); an unfinished one is closed only when named")
    p.add_argument("--yes", action="store_true", help="close the listed sessions (default: only list them)")
    p.set_defaults(func=cmd_reclaim)
    p = sub.add_parser("wait")
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--stall-after", type=float, default=300,
                   help="seconds a worker's screen may stay unchanged before it is reported as stalled")
    p.set_defaults(func=cmd_wait)
    p = sub.add_parser("relay", help="push controller messages into a Codex or OpenCode controller session")
    p.add_argument("--codex", action="store_true",
                   help="relay into the Codex session this runs in ($CODEX_THREAD_ID)")
    p.add_argument("--codex-thread", help="the Codex controller's session UUID, checked against its rollout")
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
