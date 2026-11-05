# Completion hook probes, 2026-11-05

Question: does a worker's completion hook fire only when the work is done, or also when the worker stops to ask a person something?

The hooks for this probe live in `.probe/p3/`, which is ignored. Global configuration was not changed. Each hook runs `log-hook.sh`, which appends its stdin JSON to `.probe/p3/hooks.log`.

## Claude Code 2.1.318 (interactive window in tmux, `--settings .probe/p3/claude-settings.json`)

Hooks: `Stop` and `Notification`.

| Case | How the turn ended | Hook fired | Payload |
| --- | --- | --- | --- |
| A, done | Answered `PROBE-3A done` | `Stop`, 01:27:17 | `last_assistant_message: "PROBE-3A done"`, `background_tasks: []` |
| B, plain-text question | Asked "Which color do you prefer: red or blue?" and ended the turn | `Stop`, 01:28:29 | `last_assistant_message: "Which color do you prefer: red or blue?"` |
| C, `AskUserQuestion` | Dialog opened; turn still in progress | No `Stop`. `Notification` fired at 01:29:07, about 11 s after the prompt | `notification_type: "permission_prompt"`, `message: "Claude needs your permission"` |

`Stop` payload fields: `session_id`, `transcript_path`, `cwd`, `prompt_id`, `permission_mode`, `effort`, `hook_event_name`, `stop_hook_active`, `last_assistant_message`, `background_tasks` and `session_crons`.

### Findings

1. **`Stop` cannot tell "done" from "asked a question".** Cases A and B produce the same event; only the free text differs. The `notify_when_idle` notice from the earlier probe has the same limit.
2. **A structured question does not end the turn.** With `AskUserQuestion`, `Stop` stays silent, and `Notification` reports that the session needs a person. The `notification_type` is `permission_prompt`, so a real permission request looks the same.
3. **`background_tasks`** lists work still running when the turn ends. A turn that ends while jobs are still running is not done, even if the worker says it is.

## Codex 0.166.0

Documented hooks: `Stop` (fields `turn_id`, `stop_hook_active` and `last_assistant_message`), plus `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SubagentStart`, `SubagentStop`, `PreModelRequest`, `PostModelRequest` and `Interrupt`. Codex has no `Notification` event.

A project hook is in `.probe/p3/codexproj/.codex/hooks.json`. Codex runs hooks only after they are trusted. Starting with `--dangerously-bypass-hook-trust` was refused by this session's permission classifier. The user then had the TUI started with `--dangerously-bypass-approvals-and-sandbox`. That flag does **not** skip hook review: Codex still showed "Hooks need review". The user attached to the tmux pane and trusted the hook personally.

Interactive TUI in tmux, `gpt-6.1-sol`, `permission_mode: bypassPermissions`:

| Case | How the turn ended | Hook fired | Payload |
| --- | --- | --- | --- |
| A, done | Answered `PROBE-3A done` | `Stop`, 01:36 | `turn_id …81275f`, `last_assistant_message: "PROBE-3A done"` |
| B, plain-text question | Asked "Which color do you prefer: red or blue?" and ended the turn | `Stop`, 01:36 | `turn_id …a8d04e`, `last_assistant_message: "Which color do you prefer: red or blue?"` |

`Stop` payload fields: `session_id`, `turn_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, `permission_mode`, `stop_hook_active` and `last_assistant_message`.

### Findings

4. **Codex has the same limit as Claude Code.** `Stop` fires both when the work is done and when the worker ends its turn with a question; only the free text differs.
5. **Hook trust is a separate gate.** The approvals-and-sandbox bypass does not cover it, so a new or changed hook needs a person to trust it once per hook.
6. Not tested: whether Codex has a structured question tool that keeps the turn open, the equivalent of `AskUserQuestion`.

## Conclusion for phase 1

The wake condition must be **"the report file exists"**, not "the session stopped".

- Each worker writes its report to the path in its task file, as `ai-coding/references/handoff.md` already requires.
- The completion hook, or the controller when it gets an idle notice, checks for that file:
  - report present: send a `worker.report` wake;
  - no report, last message ends with a question: send `worker.needs-person`, carrying the question text;
  - no report, background tasks still listed: do nothing yet.
- Workers should ask through a structured channel: `AskUserQuestion` on Claude Code, or a "question" field in the report. A plain-text question at the end of a turn can only be told apart by guessing from the text.
