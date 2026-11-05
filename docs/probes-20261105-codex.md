# Codex wake probes, 2026-11-05

Host: codex-cli 0.166.0 on Linux, signed in. The Codex window was the interactive TUI, run in a detached tmux pane (`codex --no-alt-screen -s read-only -a never -C /path/to/workflow-core`). Messages were sent from an outside shell, standing in for a worker's completion hook. No person typed during either probe.

Setup side effect: Codex asked whether to trust `/path/to/workflow-core`, and the answer was yes. Codex stores that decision in its configuration.

## Probe 4: `codex queue --resume` into a live, idle Codex window

```
codex queue --thread <session-uuid> --message "<text>" --resume
```

| Time | Event |
| --- | --- |
| 01:23 | TUI finished its first turn and went idle |
| 01:24:11 | `codex queue` sent from the outside shell (`Queued message … for thread …`) |
| ≤01:24:16 | The TUI showed the queued message as a new prompt and answered it (`PROBE-4 woke`) |

**Result: passed.** The idle window took the queued message within 5 seconds and treated it as a new turn. Nobody typed anything.

## Probe 4b: `codex queue --resume` with no window open

The tmux pane, and with it the TUI, was killed before the second message was queued.

| Time | Event |
| --- | --- |
| 01:24:26 | `codex queue … --resume` sent |
| 01:24:28 | An assistant reply (`PROBE-4B woke`) and `task_complete` appeared in the session's rollout file under `~/.codex/sessions/` |

**Result: passed.** The shared local app-server daemon resumed the session headless and answered. Reopening the session later shows the turn in its history.

## Findings

1. **Session identity:** `--thread` takes the session UUID, or an exact session name. The UUID is in the first line of the newest rollout file under `~/.codex/sessions/YYYY/MM/DD/`. A controller or worker launcher should record it when it starts the session.
2. **Same entry point, open or closed:** one `codex queue --resume` command reaches the session whether or not a window is showing it. When the window is open, the person sees the turn happen.
3. **Daemon:** `--resume` relies on Codex's shared local app-server daemon. Two `codex app-server` processes were running after the probes.
4. **Not tested yet:** what happens when the queued message arrives while the TUI is busy in a turn, or while a person is typing. Also untested: the worker-side direction, where a Codex worker's hook notifies a controller on another host (probe 3, Codex part).

## What this means for phase 1

- **Codex controller:** any worker can wake it with one command, `codex queue --thread <controller-uuid> --message "<report path>" --resume`.
- **Codex worker or reviewer:** the controller can send follow-ups, such as a fix round, into the same session. This gives session continuation with no extra tooling.
