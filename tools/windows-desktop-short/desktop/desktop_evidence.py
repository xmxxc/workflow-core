"""Bounded read-only evidence for a single identified Codex 0.164 worker."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID


class EvidenceError(ValueError):
    """Observation insufficient or binding inconsistent. Never implies safe replay."""


def _require(ok, message):
    if not ok:
        raise EvidenceError(message)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _object(line):
    try:
        obj = json.loads(line)
        _require(isinstance(obj, dict) and isinstance(obj.get("payload"), dict), "invalid rollout envelope")
        return obj
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EvidenceError("invalid or incomplete rollout record") from exc


def _same_cwd(actual, expected):
    if not isinstance(actual, str):
        return False
    a, b = Path(actual), Path(expected)
    if not a.is_absolute() or not b.is_absolute() or a.drive.lower() != b.drive.lower():
        return False
    if a == b:
        return True
    try:
        return a.samefile(b)
    except OSError:
        return False


def _worker_file(path, session, sessions_root):
    path = Path(path)
    _require(path.is_absolute(), "expected absolute worker rollout path")
    try:
        session = str(UUID(session))
    except (AttributeError, TypeError, ValueError) as exc:
        raise EvidenceError("invalid worker session id") from exc
    _require(path.name.startswith("rollout-") and path.name.endswith("-" + session + ".jsonl"),
             "rollout name differs from bound session")
    if sessions_root is not None:
        root = Path(sessions_root)
        _require(root.is_absolute() and path.resolve(strict=True).is_relative_to(root.resolve(strict=True)),
                 "rollout outside sessions root")
    # Check the file before opening. A misdirected FIFO would otherwise block the trial.
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and not path.is_symlink(), "expected regular worker rollout")
    _require(0 < info.st_size <= 96 * 1024 * 1024, "rollout size outside observation limit")
    return path, session


def read_rollout(path, *, session, cwd, dispatch, attempt, expected, closed=False, sessions_root=None):
    """Read just the named file. Only hashes, counts and positions leave this function."""
    path, session = _worker_file(path, session, sessions_root)
    _require(re.fullmatch(r'd-[0-9a-f]{8}', dispatch) and re.fullmatch(r'a-[0-9a-f]{12}', attempt),
             "invalid WFC assignment")
    first_brief = f"You are a worker dispatched by the controller (dispatch {dispatch})."
    attempt_arg = re.compile(r"--attempt\s+" + re.escape(attempt) + r"(?![a-zA-Z0-9_-])")
    out = dict(file=path.name, session=session, dispatch=dispatch, attempt=attempt,
               closed=closed, brief=[], exact=[], projected=[], turns=[], user_rows=[], assistant=[])
    marker = expected.partition(": ")[2]
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        total, number = 0, 0
        while total < before.st_size:
            line = stream.readline(min(8 * 1024 * 1024 + 1, before.st_size - total))
            _require(line and len(line) <= 8 * 1024 * 1024, "rollout record missing or too large")
            total += len(line)
            _require(line.endswith(b'\n') or (closed and total == before.st_size), "partial live rollout line")
            number += 1
            hasher.update(line)
            row = _object(line)
            kind, p = row.get("type"), row["payload"]
            sub = p.get("type")
            if number == 1:
                _require(kind == "session_meta" and p.get("id") == session and p.get("source") == "cli"
                         and _same_cwd(p.get("cwd"), cwd), "worker metadata binding failed")
                _require(p.get("cli_version") == "0.164.0", "unexpected worker rollout CLI version")
                out["version"] = p["cli_version"]
                continue
            _require(kind != "session_meta", "multiple session headers")
            if kind == "turn_context":
                observed = {}
                for key, allowed in (("model", {"gpt-6.1-sol","gpt-6.2-sol","gpt-6.2-astra"}),
                                     ("effort", {"minimal","low","medium","high","xhigh","max","ultra"}),
                                     ("approval_policy", {"never","on-request","untrusted","on-failure"})):
                    val = p.get(key)
                    if isinstance(val,str):
                        observed[key] = val if val in allowed else {"sha256":sha(val)}
                policy = p.get("sandbox_policy")
                if isinstance(policy,dict) and policy.get("type") in ("workspace-write","read-only","external-sandbox","danger-full-access"):
                    observed["sandbox_type"]=policy["type"]
                out.setdefault("turn_context",[]).append({"line":number,**observed})
            if kind == "response_item" and sub == "message":
                if p.get("role") == "assistant":
                    out["assistant"].append(number)
                if p.get("role") == "user":
                    parts = p.get("content")
                    _require(isinstance(parts, list) and all(isinstance(part,dict) for part in parts),
                             "unknown canonical user content")
                    texts = [part.get("text") for part in parts if part.get("type") == "input_text"]
                    _require(all(isinstance(text,str) for text in texts), "invalid user text part")
                    text = "".join(texts)
                    if text.startswith(first_brief) and attempt_arg.search(text):
                        out["brief"].append(number)
                    exact = bool(texts) and len(texts) == len(parts) and text == expected
                    if exact:
                        out["exact"].append(number)
                    out["user_rows"].append(dict(line=number,sha256=sha(text),chars=len(text),exact=exact,
                        contains_wrapped=expected in text, marker=bool(marker) and marker in text,
                        bare=bool(marker) and text == marker))
            if kind == "event_msg":
                if sub == "user_message" and p.get("message") == expected:
                    out["projected"].append(number)
                if sub == "item_completed":
                    item = p.get("item")
                    if isinstance(item, dict) and item.get("type") == "UserMessage":
                        parts = item.get("content")
                        if isinstance(parts, list) and parts and all(
                                isinstance(part, dict) and part.get("type") == "text" and
                                isinstance(part.get("text"), str) for part in parts):
                            if "".join(part["text"] for part in parts) == expected:
                                out["projected"].append(number)
                if sub in ("task_started","task_complete","turn_aborted","error"):
                    out["turns"].append([number, sub, p.get("turn_id"), row.get("timestamp")])
        after = os.fstat(stream.fileno())
    named = path.stat()
    _require(all((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns) ==
                 (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)
                 for s in (after,named)), "worker rollout changed during observation")
    _require(len(out["brief"]) == 1, "could not uniquely bind dispatch and attempt to worker opening brief")
    out.update(bytes=total, sha256=hasher.hexdigest())
    return out


# Retain known UI fragments, never free-form model output, task text, paths or tokens.
_SIGNALS = {
    "queued": r'\b(?:queued(?: messages?)?|messages? in (?:the )?queue|pending (?:messages?|input))\b|已排队|待提交',
    "queue-shortcut": r'\b(?:tab to queue|tab to edit|steer conversation|press up to edit)\b',
    "working": r'\bworking\b(?:\s*\(|\s*…)|\bthinking\b(?:\s*\(|\s*…)|正在(?:处理|思考)',
    "interrupt-shortcut": r'\besc to interrupt\b',
    "completed": r'\bworked for\b',
    "placeholder": r'Ask Codex to do anything',
    "footer": r'\b(?:for shortcuts|for agents|context left)\b',
    "startup-error": r'\bError adding directories\b',
    "read-only": r'\bread.only\b|input is paused',
}


def snapshot(data, router, expected):
    """Summarize one actual read. Ordered categories retain the location of visible text."""
    text = data.get("text")
    usable = (isinstance(text,str) and bool(text.strip()) and data.get("eof") is False
              and "exit" in data and data["exit"] is None and "read_error" in data
              and data["read_error"] is None and all(type(data.get(k)) is int and data[k] > 0 for k in ("rows","cols")))
    lines = text.splitlines() if isinstance(text,str) else []
    box = router.codex_input_box(text, require_complete=True) if usable else None
    composer = None if box is None else dict(empty=not box.text.strip(),exact=box.text==expected,
                                             scrolled=box.scrolled,chars=len(box.text))
    first = max(0,len(lines)-20)
    rows, queue, activity = [], [], []
    marker = expected.partition(": ")[2]
    for n,line in enumerate(lines[first:], first+1):
        labels = [label for label,rx in _SIGNALS.items() if re.search(rx,line,re.I)]
        if line.lstrip().startswith("›"):
            labels.append("prompt-glyph")
        if expected in line:
            labels.append("wrapped-reply")
        elif marker and marker in line:
            labels.append("answer-marker")
        if "queued" in labels or "queue-shortcut" in labels:
            queue.append(n)
        if any(label in labels for label in ("working","interrupt-shortcut")):
            activity.append(n)
        rows.append(dict(row=n,chars=len(line),labels=labels or (["other"] if line.strip() else ["blank"])))
    terminal=dict(rows=data.get("rows"),cols=data.get("cols"),eof=data.get("eof"),
                  screen=data.get("screen") if data.get("screen") in ("Primary","Alternate") else None,
                  exited=data.get("exit") is not None,read_error=bool(data.get("read_error")))
    return dict(usable=bool(usable), bottom=rows,queue_hints=queue,activity_hints=activity,composer=composer,
                ready_to_reply=bool(usable and composer and composer["empty"] and not composer["scrolled"]
                                    and not queue and not activity), terminal=terminal)


def input_observation(text, expected):
    if text.startswith("\x1b[200~") and text.endswith("\x1b[201~"):
        value=text[6:-6]
        return dict(kind="paste",sha256=sha(text),payload_sha256=sha(value),exact=value==expected)
    return dict(kind="enter" if text=="\r" else "other",sha256=sha(text))


def check_question(message, assignment, question="D_GO?"):
    _require(isinstance(message,dict) and all(k in assignment and message.get(k) == assignment[k]
             for k in ("dispatch","attempt","round","scope")), "question assignment mismatch")
    _require(message.get("type")=="question" and message.get("question")==question and
             type(message.get("id")) is int and message["id"]>0 and isinstance(message.get("messageId"),str)
             and re.fullmatch(r'm-[0-9a-f]{32}',message["messageId"]) and message.get("state")=="received"
             and type(message.get("statusVersion")) is int and message["statusVersion"] >= 2,
             "question receipt mismatch")
    return {k:message[k] for k in ("dispatch","attempt","round","scope","id","messageId","state","statusVersion")}


def check_reply(receipt, assignment, question_id):
    _require(isinstance(receipt,dict) and type(receipt.get("replied")) is int and receipt["replied"]==question_id
             and receipt.get("dispatch")==assignment["dispatch"], "reply receipt mismatch")
    return {k:receipt[k] for k in ("dispatch","replied")}
