# Claude Code wake probes, 2026-11-05

Host: Claude Code 2.1.318 on Linux. The controller was a `claude --bg` session in auto mode. No user input arrived during either probe.

## Probe 1: a background command exit wakes the idle controller

The controller started a background Bash command that waited for `.probe/event`. A detached process (`setsid nohup`), standing in for a worker, wrote the file after 20 seconds. The controller then ended its turn.

| Time | Event |
| --- | --- |
| 01:17:50 | Waiter armed; turn ended |
| 01:18:10 | Fake worker wrote the event; waiter exited |
| ~01:18:14 | Controller woke with the task notification and the event content |

**Result: passed.** The controller woke within seconds, with no polling and no user input.

**Implication:** a worker on any host can wake a Claude Code controller by writing an event file, as long as the controller re-arms a waiter before each turn ends.

## Probe 2: peer messaging between two Claude Code sessions

The controller started a second session with `claude --bg -n probe2-worker`. It sent that session a task with `SendMessage` and set `notify_when_idle: true`. The task asked the worker to reply with `SendMessage`.

| Time | Event |
| --- | --- |
| 01:18:53 | Worker launched; it stayed idle (see findings) |
| 01:19:18 | Task sent; controller subscribed to the worker's idle notice; turn ended |
| 01:19:35 | Controller woke with the worker's reply and a `[Cross-session idle notice]` |

**Result: passed in both directions.** A message woke an idle peer, the peer's reply woke the idle controller, and the idle subscription delivered a completion notice without polling.

## Findings

1. **`claude --bg "<prompt>"` does not run the prompt.** The session starts idle ("send a prompt to start"). The first task has to be delivered by `SendMessage`, or by attaching.
2. **`--bg` needs a trusted workspace.** It refused `/path/to/workflow-core` until that directory had been trusted interactively once. Run `claude` there once and accept the prompt.
3. **`notify_when_idle` means idle, not done.** It fires whenever the worker finishes a turn, including a turn that ends with a question for a person. The controller must still check for the worker's report file to tell "done" from "waiting". This is the false-trigger concern (probe 3) for Claude Code workers.
4. **The subscription is one-shot.** The controller has to subscribe again after every task it sends.
5. **Permission classes:** the messages arrived marked `from-mode="prompting"`. Per the tool's documentation, a session in a different permission mode may hold cross-session messages for its user's approval. Workers and the controller should run in the same permission mode.

## What this means for phase 1

- **Claude Code controller with Claude Code workers:** no script is needed. Use `SendMessage` with `notify_when_idle: true` for each dispatch, and check the report file on each notice.
- **Claude Code controller with Codex or OpenCode workers:** the worker's completion hook writes an event file, and the controller keeps a background waiter on it (probe 1).
- Still to test: whether a completion hook fires early on workers that stop to ask a question (probe 3), `codex queue --resume` into a live Codex TUI (probe 4), and OpenCode `prompt_async` (probe 5).
