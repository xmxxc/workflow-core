# Orca orchestration probes, 2026-11-05

Host: Orca 1.4.249 (stablyai/orca, MIT), the Linux AppImage at `~/.local/path/to/orca/orca-linux.AppImage` (sha512 checked against the release's `latest-linux.yml`). The desktop app ran on the xrdp display `:10`. The tests drove it only through its CLI. Agents: Codex 0.166.0, Claude Code 2.1.318, and OpenCode 1.19.5 with DeepSeek.

The coordinator was this Claude Code session, which does **not** run inside Orca. It acted through an Orca terminal created as a "coordinator mailbox" (`orca terminal create --worktree path:/path/to/workflow-core`). Its wait was a background Bash `orchestration check --wait`, so the wake path is the one from probe 1.

## Setup facts

- **CLI path:** `~/.config/orca/linux-orca-cli-shim/orca`. It points at the current AppImage mount and is rewritten on every launch. `/usr/bin/orca` is GNOME's Orca screen reader, an unrelated program; never call it.
- **Launch detached.** Started from a terminal in the foreground, Orca's login-shell probe (`bash -ilc`) took the terminal, and the whole Orca process group was stopped (`STAT T`). The UI froze and the CLI returned `runtime_timeout`. Start it with `DISPLAY=:10 setsid -f ~/.local/path/to/orca/orca-linux.AppImage --no-sandbox </dev/null >~/.local/path/to/orca/orca.log 2>&1`. A headless `orca serve` mode exists (docs: Remote Orca Servers) and was not tested.
- **First launch clones `stablyai/orca-plugins`** (about 31 MB). Over the proxy this took more than 6 minutes.
- **Global config changed by Orca:** it added 15 hook events and a statusLine to `~/.claude/settings.json` (original kept as `settings.json.bak`), created `~/.codex/hooks.json`, and wrote helpers to `~/.orca/agent-hooks/`. The hooks do nothing unless Orca's environment variables are set.
- Orchestration commands worked without turning on a UI switch. They need a sender identity: run inside an Orca terminal (`ORCA_TERMINAL_HANDLE`), or pass `--from <handle>`. `check` uses `--terminal <handle>`. **Flags differ per subcommand**, and an unknown flag returns `ok:false` with **exit code 0**, so a wrapper must check `ok`, not the exit code.
- The repo was registered with `orca repo add --path /path/to/workflow-core --kind git`.

## Results

| Probe | Worker | Flow | Timing | Result |
| --- | --- | --- | --- | --- |
| 6 | Codex | task → work → `worker_done` | completed within 10 s | ✅ structured `worker_done` (`outcome: succeeded`); the task became `completed`. The first wait used a wrong flag and returned at once (see Setup facts) |
| 7 | Claude Code | task → work → `worker_done` → wakes the coordinator | started 02:24:26, coordinator woke 02:24:35 | ✅ |
| 8a | OpenCode, default model | task → … | stalled for more than 5 min | ❌ Orca launched OpenCode with the free `opencode/big-pickle` model, which failed with "Free usage limit reached". **No failure or escalation reached the coordinator**: `worker-show` reported `state: ready`, `liveness: unverifiable` |
| 8b | OpenCode, `--model deepseek/deepseek-flash` (`--retry-of`) | task → `ask` (question with options) → coordinator woke → `reply blue` → worker woke → `worker_done` | retried 02:30:33 → question 02:30:44 → reply 02:31:03 → done 02:31:07 | ✅ full two-way sleep/wake loop |

Message examples:

```json
{"type":"question","body":"Wake probe: should I answer red or blue? Options: red, blue",
 "payload":{"taskId":"task_473482fa10d5","dispatchId":"ctx_52f9c19f102e","options":["red","blue"]}}
{"type":"worker_done","subject":"Probe complete: PROBE-8 blue",
 "payload":{"taskId":"task_473482fa10d5","dispatchId":"ctx_52f9c19f102e","outcome":"succeeded"}}
```

## Findings

1. **Orca covers phase 1 and most of phase 2.** It runs official CLIs on their own logins, mixes Codex, Claude and OpenCode workers, and has structured done and question messages. Workers sleep after `ask`, and Orca wakes them with the answer.
2. **Orca removes the false-completion problem** from the completion-hook probes. Workers report with explicit `worker_done` and `ask` messages, and the injected worker brief forbids local question prompts.
3. **Coordinator wake:** a coordinator outside Orca wakes reliably through a background `orchestration check --wait`. A coordinator inside an Orca terminal is woken by Orca itself, according to Orca's own `next` hint; that was not tested here.
4. **Silent worker failure is the main gap.** A worker whose model fails sends nothing. The coordinator must:
   - always pass `--model` for OpenCode workers;
   - set a finite `--timeout-ms` on `check --wait`;
   - on timeout, inspect `worker-show` and `worker-read`, then `worker-stop` and retry with `--retry-of`.
5. Each worker's transcript is archived on `worker-release`.

## Implications for workflow-core

- `notify_controller.py` (branch `wip/notify-controller`) is not needed while Orca is used. Keep it as the fallback for running without Orca.
- Next work is the `ai-coding` side: a dispatch mode that maps tickets onto `orchestration task-create` and `worker-start` and records `taskId` and `dispatchId` in the ledger. The independent reviewer is a separate dispatch with a different agent. A timeout watchdog follows finding 4.
