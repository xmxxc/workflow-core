#!/usr/bin/env python3
"""The controller's routine checks between rounds, so each round runs them the same way.

  checks      move a read-only review snapshot to a head and run the project's checks,
              logging each command's real exit code
  candidate   check a pushed candidate: remote head, tree, no force-push, no committed secret,
              no closing keywords, record comments exist, the worker's port is free
  transcribe  post one section of an offline reviewer's report to the PR, attributed
  label       switch a lifecycle label and read it back

Every command prints one JSON object and exits 1 when a check fails. Secret values are never
printed. Uses git and gh; Python standard library only.
"""

import argparse
import fcntl
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

CLOSING = re.compile(r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\s*:?\s*#\d+", re.IGNORECASE)


def emit(obj, ok=True):
    print(json.dumps(obj, ensure_ascii=False, indent=1))
    return 0 if ok else 1


def git(cwd, *args, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def cmd_checks(args):
    """Hold the snapshot for the whole run, so a parallel `checks` cannot move it to another head
    while this one checks. The lock lives in the snapshot's git directory, outside the work tree."""
    cwd = Path(args.cwd)
    git_dir = Path(git(cwd, "rev-parse", "--absolute-git-dir").stdout.strip())
    with open(git_dir / "wfc-checks.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return run_checks(args, cwd)


def run_checks(args, cwd):
    # Untracked files count too: they would be checked as if they were part of the recorded tree.
    if git(cwd, "status", "--porcelain").stdout.strip():
        raise SystemExit(f"{cwd} has local changes or untracked files; a review snapshot must stay read-only")
    git(cwd, "fetch", "-q", "origin", check=False)  # offline is fine when the head is already here
    git(cwd, "checkout", "-q", "--detach", args.head)
    head = git(cwd, "rev-parse", "HEAD").stdout.strip()
    tree = git(cwd, "rev-parse", "HEAD^{tree}").stdout.strip()
    results = []
    with open(args.log, "w") as log:
        log.write(f"# snapshot {head} tree {tree} — run by controller {datetime.now().astimezone().isoformat()}\n")
        for command in args.cmd:
            # pipefail: a `... | tail` must report the command's failure, not tail's success.
            # One merged stream keeps the order: a test summary on stdout is not pushed out by stderr logs.
            run = subprocess.run(["bash", "-o", "pipefail", "-c", command], cwd=cwd, text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            tail = "\n".join(run.stdout.rstrip().splitlines()[-args.tail:])
            log.write(f"## $ {command}\n{tail}\n## exit={run.returncode}\n")
            log.flush()
            results.append({"cmd": command, "exit": run.returncode})
    ok = all(r["exit"] == 0 for r in results)
    return emit({"head": head, "tree": tree, "log": str(Path(args.log).resolve()), "results": results, "ok": ok}, ok)


def secret_value(path, key):
    for line in Path(path).read_text().splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return value.strip().strip("'\"")
    raise SystemExit(f"{key} not found in {path}")


def cmd_candidate(args):
    repo = Path(args.git)
    git(repo, "fetch", "-q", "origin")
    head = git(repo, "rev-parse", args.head).stdout.strip()
    remote = git(repo, "rev-parse", f"origin/{args.branch}", check=False).stdout.strip()
    checks = {"remote-head": {"ok": remote == head, "detail": f"origin/{args.branch} is {remote or 'missing'}"}}
    tree = git(repo, "rev-parse", f"{head}^{{tree}}").stdout.strip()
    if args.since:
        ancestor = git(repo, "merge-base", "--is-ancestor", args.since, head, check=False).returncode == 0
        checks["no-force-push"] = {"ok": ancestor, "detail": f"{args.since[:12]} {'is' if ancestor else 'is not'} an ancestor"}
    if args.secret_file:
        value = secret_value(args.secret_file, args.secret_key)
        found = git(repo, "grep", "-l", "-F", "-e", value, head, check=False)
        files = [line.split(":", 1)[-1] for line in found.stdout.splitlines()]
        # git grep: 0 = found, 1 = not found, anything else = the scan itself failed (fail closed).
        detail = {0: f"{args.secret_key} value found in: {', '.join(files)}", 1: "not committed"}.get(
            found.returncode, f"git grep failed (exit {found.returncode}); scan not trusted")
        checks["no-secret"] = {"ok": found.returncode == 1, "detail": detail}
    if args.pr:
        view = gh("pr", "view", str(args.pr), "--repo", args.repo, "--json", "body,commits")
        if view.returncode != 0:
            checks["no-closing-keywords"] = {"ok": False, "detail": f"gh pr view failed: {view.stderr.strip()}"}
        else:
            pr = json.loads(view.stdout)
            texts = [pr.get("body") or ""] + [f"{c.get('messageHeadline', '')}\n{c.get('messageBody', '')}"
                                              for c in pr.get("commits", [])]
            hits = sorted({m.group(0) for t in texts for m in CLOSING.finditer(t)})
            checks["no-closing-keywords"] = {"ok": not hits, "detail": ", ".join(hits) or "none"}
    if args.comment:
        missing = [c for c in args.comment
                   if gh("api", f"repos/{args.repo}/issues/comments/{c}").returncode != 0]
        checks["comments"] = {"ok": not missing, "detail": f"missing: {', '.join(missing)}" if missing else "all found"}
    if args.port:
        with socket.socket() as probe:
            probe.settimeout(1)
            busy = probe.connect_ex(("127.0.0.1", args.port)) == 0
        checks["port-free"] = {"ok": not busy, "detail": f"port {args.port} {'in use' if busy else 'free'}"}
    ok = all(c["ok"] for c in checks.values())
    return emit({"head": head, "tree": tree, "checks": checks, "ok": ok}, ok)


def section(text, start, end):
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith(start)), None)
    if first is None:
        raise SystemExit(f"no line starting with {start!r}")
    last = next((i for i in range(first + 1, len(lines)) if end and lines[i].startswith(end)), len(lines))
    return "\n".join(lines[first:last]).rstrip()


def source_label(report):
    top = git(report.parent, "rev-parse", "--show-toplevel", check=False).stdout.strip()
    return str(report.relative_to(top)) if top else str(report)


def cmd_transcribe(args):
    report = Path(args.report).resolve()
    body = section(report.read_text(), args.start, args.end)
    header = (f"> 总控转录：原作者为 {args.author}，来源为总控账本 `{source_label(report)}`。"
              f"{args.why}由总控代为发布，正文未改动。\n\n")
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(header + body + "\n")
    try:
        kind = ["pr", "comment", str(args.pr)] if args.pr else ["issue", "comment", str(args.issue)]
        posted = gh(*kind, "--repo", args.repo, "--body-file", f.name)
    finally:
        Path(f.name).unlink()
    url = posted.stdout.strip().splitlines()[-1] if posted.stdout.strip() else ""
    if posted.returncode != 0 or "#issuecomment-" not in url:
        # GitHub sometimes answers 502/EOF after the comment was created: look before retrying.
        raise SystemExit(f"gh comment failed ({posted.stderr.strip()}); check the thread before posting again")
    return emit({"url": url, "lines": body.count("\n") + 1})


def cmd_label(args):
    labels = []
    for attempt in range(2):
        gh("issue", "edit", str(args.issue), "--repo", args.repo, "--remove-label", args.from_label,
           "--add-label", args.to)
        view = gh("issue", "view", str(args.issue), "--repo", args.repo, "--json", "labels",
                  "--jq", '.labels|map(.name)|join(",")')
        labels = [x for x in view.stdout.strip().split(",") if x]
        if args.to in labels and args.from_label not in labels:
            return emit({"labels": labels})
        time.sleep(3)  # GitHub's 502/EOF: the edit may or may not have landed, so read back and retry once
    return emit({"labels": labels, "error": f"expected {args.to} without {args.from_label}"}, ok=False)


def main(argv):
    parser = argparse.ArgumentParser(prog="ctl.py", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("checks", allow_abbrev=False)
    p.add_argument("--cwd", required=True, help="the review snapshot worktree")
    p.add_argument("--head", required=True)
    p.add_argument("--log", required=True)
    p.add_argument("--cmd", action="append", required=True, help="one check command; repeat in order")
    p.add_argument("--tail", type=int, default=25, help="output lines kept per command")
    p.set_defaults(func=cmd_checks)
    p = sub.add_parser("candidate", allow_abbrev=False)
    p.add_argument("--git", required=True, help="a local clone whose origin is the PR repository")
    p.add_argument("--repo", required=True, help="owner/name")
    p.add_argument("--branch", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--since", help="the last reviewed candidate; must be an ancestor of --head")
    p.add_argument("--pr", type=int)
    p.add_argument("--secret-file")
    p.add_argument("--secret-key", default="PGPASSWORD")
    p.add_argument("--comment", action="append", help="a record comment id that must exist; repeatable")
    p.add_argument("--port", type=int, help="a port the worker must have released")
    p.set_defaults(func=cmd_candidate)
    p = sub.add_parser("transcribe", allow_abbrev=False)
    p.add_argument("--repo", required=True)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int)
    target.add_argument("--issue", type=int)
    p.add_argument("--report", required=True)
    p.add_argument("--start", required=True, help="the section's first line starts with this")
    p.add_argument("--end", help="the section stops before a line starting with this (default: end of file)")
    p.add_argument("--author", required=True, help="who wrote it, e.g. Codex 审查者（router dispatch `d-…`）")
    p.add_argument("--why", default="审查者的沙箱不能联网，", help="why the controller posts it")
    p.set_defaults(func=cmd_transcribe)
    p = sub.add_parser("label", allow_abbrev=False)
    p.add_argument("--repo", required=True)
    p.add_argument("--issue", type=int, required=True)
    p.add_argument("--from", dest="from_label", required=True)
    p.add_argument("--to", required=True)
    p.set_defaults(func=cmd_label)
    args = parser.parse_args(argv)
    if args.command == "candidate" and args.secret_file and not args.secret_key:
        parser.error("--secret-file needs --secret-key")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
