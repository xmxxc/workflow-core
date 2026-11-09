# workflow-core

A lightweight router between the controller and the workers it dispatches, shipped as one command, `wfc`. The controller skill that drives it, `super-coding` (with its routine-check script), is published separately in [xmxxc/super-coding](https://github.com/xmxxc/super-coding). Workers report or ask through `wfc` and then sleep. The controller sleeps on `wfc wait` and is woken by their messages.

## Status

Phase 1 is functional. `router.py` dispatches Codex, OpenCode and Claude Code workers. A Claude Code controller sleeps on `wait`; a Codex or OpenCode controller is woken by `relay` (through `codex queue`, or OpenCode's `prompt_async` HTTP API). `continue` gives a worker its next task in the same session, so a fix round reuses the implementer and an incremental review reuses the reviewer. While waiting, the router watches its workers and reports `exited` (the session ended without `done`) or `stalled` (the screen was unchanged for `--stall-after` seconds with no pending question), with the screen tail attached. When a worker's turn ends on a host error (a used-up quota, an overloaded model, a policy refusal), it reports `blocked` at once with the host's own error kind and message, read from the host's conversation record rather than the screen. A worker's `done` carries `head` (`git rev-parse HEAD` in the dispatch's worktree) and `dirty` (uncommitted or untracked changes there), both `null` when that is not a git repository or git fails or a git call takes over 5 seconds (each git call has its own 5-second limit), so the controller can check the head its report names; `status` shows them on the last message. `nudge` types one line into a worker still on its task (to continue after the quota is back, say), recorded in router.db. `status` shows every dispatch, the messages not yet delivered and the running `wait` or `relay`, without taking any message. It is fully read-only: it opens router.db read-only, shows an empty state when there is none, and shows an older database as far as its schema goes (noting so under `schema`); only write commands create or upgrade it. A running `wait` or `relay` is recorded with its process start time, so a pid reused by another process is not listed. `resume` relaunches a worker whose session ended into its own host conversation, so it keeps the context of earlier rounds. Three tickets in xmxxc/example-project were taken through dispatch, review, release and live acceptance this way. `notify_controller.py`, the earlier hook-based classifier, has been replaced by the watchdog and is kept for reference.

Known limits:
- `relay` never queues a batch twice: when `codex queue` times out or exits non-zero, it may already have taken the batch, so delivery is uncertain: the batch is logged in `relay.log` and not retried (check the controller session). Only a queue that could not start at all (codex missing) is retried.
- `reply` and `continue` likewise never type a line twice: when the text reached the worker but its Enter was not confirmed, they stop and ask you to attach and press Enter.
- `codex queue` also succeeds for a thread id that does not exist, so a relay started with the wrong UUID delivers nothing. Confirm that the first relayed message arrives.
- `resume` finds a worker's conversation by its first brief: Codex rollouts under `$CODEX_HOME/sessions` (default `~/.codex`), OpenCode's `opencode.db` (`$WFC_OPENCODE_DB`, default under `$XDG_DATA_HOME`), and for Claude Code the `--session-id` the router gives it at dispatch. These are host internals that may change. All three have been verified against real hosts: an OpenCode worker after Ctrl-C, and a Codex and a Claude Code reviewer that had quit, each resumed with its earlier rounds in context. OpenCode ignores `--prompt` on a reopened session, so the router types the brief in once the screen settles.
- `blocked` reads the same records (and Claude Code's transcript under `$CLAUDE_CONFIG_DIR`, default `~/.claude`): the latest turn end of a Codex rollout (`task_complete.error`), a Claude Code transcript (`isApiErrorMessage`) or an OpenCode session (`message.data.error`). An error from before the worker's latest dispatch, continue, resume or nudge is not reported. Anything unreadable counts as no record, and the screen watchdog remains the fallback.
- A running Claude Code session rewrites `~/.claude.json` from memory and can drop a trust entry the router just wrote. If that happens, the worker stops on the trust prompt and is reported `stalled`.

## Install

Requirements: Linux or macOS (on Windows, use WSL2), python3 (3.9+), tmux, and the host CLIs you dispatch to (Codex, Claude Code, OpenCode).

1. Download `wfc` from this repository's Releases, put it on your PATH and make it executable:
   `install -m 755 wfc ~/.local/bin/wfc` (check with `wfc --version`).
2. For the development workflow on top of it, install the `super-coding` skills from xmxxc/super-coding (see its README).

To build `wfc` from a checkout: `python3 build.py` writes `dist/wfc`.

CI (`.github/workflows/ci.yml`) runs the tests and a build on Ubuntu and macOS with Python 3.9 and 3.12 for every push to main and every pull request. Releases (`.github/workflows/release.yml`): set `VERSION` in `wfc.py`, then push a matching tag (`v0.1.0`); the workflow tests, builds `wfc` with its SHA-256 and publishes them as a GitHub Release. Verify a download with `sha256sum -c wfc.sha256`.

## Layout

- `router.py`: the dispatcher (Python standard library only). `tests/test_router.py` covers it through its command line.
- `wfc.py`: the single command; `build.py` packs it with `router.py` into the one-file `wfc` (a Python zip application). `tests/test_wfc.py` covers the build.
- `docs/decision-log.md`: user decisions, controller decisions and lessons; `docs/probes-*.md`: host capability probes behind the design. These also record the design of the `super-coding` skill.

## Usage

```sh
# controller
wfc dispatch --host codex|opencode|claude --task <task.md> --cwd <worktree> --report <report.md> [--model <model>] [--effort <level>] [--network] [--git] [--label "<project> | <ticket> | <role>"]
wfc wait --timeout 900 [--stall-after 300]   # blocks until a worker message (done, question, blocked, stalled, exited); exit code 3 on timeout
wfc status [--dispatch <id>]   # read-only (never creates or upgrades router.db): dispatches, undelivered messages, running waiters; --dispatch adds its message history
wfc nudge <dispatch> --text "<one line>"   # type a line into a worker that has not finished its task
wfc reply <question-id> "<answer>"
wfc continue <dispatch> --task <next-task.md> --report <next-report.md>   # next round in the same session (report in the same dir)
wfc resume <dispatch> [--task <next-task.md> --report <next-report.md>]   # session ended: relaunch into its own conversation; --task only after done
wfc relay --codex-thread <session-uuid> --detach [--stall-after 300]   # Codex controller: push messages into its session instead of `wait`; prints the pid
wfc relay --opencode-url http://127.0.0.1:<port> --opencode-session <ses_id> --detach   # OpenCode controller (started with --port): push messages over its HTTP API

# worker (the dispatch brief tells it these exact commands)
wfc done --report <report.md>   # the done message adds the worktree's head and dirty
wfc ask "<question>" --options a,b
```

Workers run unattended. They may write only their worktree, the router state, and the report's directory: Codex uses `-s workspace-write -a never`; OpenCode allows every external directory, because its directory rules are tool prompts rather than a sandbox (shell writes bypass them) and git or pnpm would otherwise stop it constantly; Claude Code runs in `auto` mode with `--add-dir`. Claude Code asks for trust on every new path, worktrees included, so `dispatch --host claude` pre-trusts the worktree in `~/.claude.json`, but only when the user already trusts one of its parent directories (`trustedBy` in the output); otherwise the worker stops on the prompt and is reported `stalled`. Pin `--model` for OpenCode, because its default model can be a rate-limited free one. `--effort` sets the reasoning effort through each host's own option: Codex `-c model_reasoning_effort=<level>`, Claude Code `--effort <level>`, OpenCode `--variant <level>` (levels are the host's own). `--network` lets a Codex worker's sandbox reach the network (`-c sandbox_workspace_write.network_access=true`); Claude Code and OpenCode are not network-sandboxed, so it is ignored for them and `dispatch` says so under `ignored`. Both are recorded with the dispatch and kept by `resume`, so no worktree needs its own `.codex/config.toml`. A Codex worker in a linked worktree cannot `git commit` in its sandbox when the worktree's git directories lie outside all of its writable directories (the worktree, `/tmp`, `$TMPDIR` and the added ones), as they usually do; adding them made commits work in the layout and version measured in [the probe](docs/probes-20261109-codex-git.md). `--git` (off by default) does that: it adds the worktree's `git rev-parse --git-dir` and `--git-common-dir`, resolved to absolute paths, to the Codex worker's `--add-dir`, and refuses to dispatch when the worktree is not a git repository. Its boundary: the worker can then change anything under the main repository's `.git` (other branches, refs, config), not only commit to its own branch. Use it for local commits only; pushing, opening PRs and posting comments stay with the controller (the sandbox has no network by default, and no credentials). Claude Code and OpenCode can write `.git` anyway, so `--git` is ignored for them and listed under `ignored`. It is recorded with the dispatch, kept by `resume` and shown by `status`. Each worker runs in its own tmux session, `wfc-<dispatch id>`, on the controller's tmux server: the watchdog treats a missing session as `exited`, so the controller and its workers must use the same tmux server and `TMUX_TMPDIR`. Each dispatch has a label, `<project> | <ticket> | <role>`: by default the project is the main repository's directory (the parent of the worktree's `git rev-parse --git-common-dir`, or the worktree's own name outside git), and the ticket and role come from the report directory, `reports/<ticket>-<role>/`, split at its last `-` (a name without `-` is kept whole). `--label` overrides it; newlines and other control characters become spaces, and a label left empty by that gets the default. The label is kept in router.db and shown by `status`, names the tmux window (the session stays `wfc-<id>`; tmux stops renaming it, and the host cannot rename it unless the user's tmux turns on `allow-rename`), and is the Claude Code session name (`--name`, kept on `resume`). Codex and OpenCode titles are not set: neither has a launch option for it (OpenCode's `--title` is only on `opencode run`), and both can only be renamed once the session exists, through host-internal interfaces (Codex app-server `thread/name/set`, OpenCode `PATCH /session/{id}`); a later version could do that from the watchdog ([probe](docs/probes-20261109-session-title.md)). Use `tmux attach -t wfc-<id>` to watch it or step in, and leave with Ctrl-b d. Some terminals (VS Code's, for one) take Ctrl-b for themselves; then run `tmux detach-client -s wfc-<id>` from another shell. Ctrl-C would stop the worker; `resume` brings it back. The brief also asks the worker to stop any server or background process it started before it reports. State is kept in SQLite under `$WFC_STATE`, which defaults to `~/.local/state/workflow-core`. Run the tests with `python3 -m unittest discover -s tests`.

## Scope by phase

1. **Completion wake**: a finished worker or reviewer wakes the controller window through its host's own entry point. Neither a resident scheduler nor a headless controller is required.
2. Different hosts for different roles: GPT runs in Codex CLI, Claude in Claude Code, and other models in OpenCode.
3. Independent run: the controller scans `ready-for-agent` issues, in observe mode first.
4. Lighter controller: mechanical work moves into code.
5. More wake sources: Hermes gateway, MCP and a panel.

Each phase adds to the previous one, and each phase can be used on its own.

## Documents

- [Handoff, 2026-11-04](docs/handoff-20261104-completion-wake.md): decisions made so far, the host wake matrix, the probes to run first, and the directions that were rejected.
- `docs/probes-20261105-*.md`: measured wake and completion behaviour of Claude Code, Codex, OpenCode and Orca.
- [Session title probe, 2026-11-09](docs/probes-20261109-session-title.md): how each host can be given a session title at launch.
- [Codex git probe, 2026-11-09](docs/probes-20261109-codex-git.md): `git commit` from a linked worktree in Codex's `workspace-write` sandbox, with and without its git directories in `--add-dir`.
