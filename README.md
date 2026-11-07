# workflow-core

A lightweight router between the controller and the workers it dispatches, for the `ai-coding` workflow maintained in `/path/to/skills-repo`. Workers report or ask through the router and then sleep. The controller sleeps on `router.py wait` and is woken by their messages.

## Status

Phase 1 is functional. `router.py` dispatches Codex, OpenCode and Claude Code workers. A Claude Code controller sleeps on `wait`; a Codex controller is woken by `relay`. `continue` gives a worker its next task in the same session, so a fix round reuses the implementer and an incremental review reuses the reviewer. While waiting, the router watches its workers and reports `exited` (the session ended without `done`) or `stalled` (the screen was unchanged for `--stall-after` seconds with no pending question), with the screen tail attached. `resume` relaunches a worker whose session ended into its own host conversation, so it keeps the context of earlier rounds. Three tickets in xmxxc/example-project were taken through dispatch, review, release and live acceptance this way. `notify_controller.py`, the earlier hook-based classifier, has been replaced by the watchdog and is kept for reference.

Known limits:
- `relay` never queues a batch twice: when `codex queue` times out, delivery is uncertain, so the batch is logged in `relay.log` and not retried (check the controller session). A queue that clearly failed (codex missing, non-zero exit) is retried.
- `codex queue` also succeeds for a thread id that does not exist, so a relay started with the wrong UUID delivers nothing. Confirm that the first relayed message arrives.
- `resume` finds a worker's conversation by its first brief: Codex rollouts under `$CODEX_HOME/sessions` (default `~/.codex`), OpenCode's `opencode.db` (`$WFC_OPENCODE_DB`, default under `$XDG_DATA_HOME`), and for Claude Code the `--session-id` the router gives it at dispatch. These are host internals that may change. All three have been verified against real hosts: an OpenCode worker after Ctrl-C, and a Codex and a Claude Code reviewer that had quit, each resumed with its earlier rounds in context. OpenCode ignores `--prompt` on a reopened session, so the router types the brief in once the screen settles.
- A running Claude Code session rewrites `~/.claude.json` from memory and can drop a trust entry the router just wrote. If that happens, the worker stops on the trust prompt and is reported `stalled`.

## Layout and installation

- `router.py`: the dispatcher (Python standard library only). `tests/test_router.py` covers it through its command line.
- `ctl.py`: the controller's routine checks between rounds (snapshot checks with real exit codes, candidate read-back, transcribing an offline reviewer's record, label switch). `tests/test_ctl.py` covers it with real git and a fake `gh`.
- `skills/surper-coding`: the controller skill that uses the router. Install it by linking it into each host's skills directory:
  `ln -s "$PWD/skills/surper-coding" ~/.claude/skills/surper-coding` and `ln -s "$PWD/skills/surper-coding" ~/.codex/skills/surper-coding`.
- `skills/ai-coding`, `skills/live-verify`: pinned copies of the prototypes `surper-coding` builds on; see [skills/VENDORED.md](skills/VENDORED.md). They are not installed from here.
- `docs/decision-log.md`: user decisions, controller decisions and lessons; `docs/probes-*.md`: host capability probes behind the design.

## Usage

```sh
# controller
router.py dispatch --host codex|opencode|claude --task <task.md> --cwd <worktree> --report <report.md> [--model <model>]
router.py wait --timeout 900 [--stall-after 300]   # blocks until a worker message (done, question, stalled, exited); exit code 3 on timeout
router.py reply <question-id> "<answer>"
router.py continue <dispatch> --task <next-task.md> --report <next-report.md>   # next round in the same session (report in the same dir)
router.py resume <dispatch> [--task <next-task.md> --report <next-report.md>]   # session ended: relaunch into its own conversation; --task only after done
router.py relay --codex-thread <session-uuid> --detach [--stall-after 300]   # Codex controller: push messages into its session instead of `wait`; prints the pid

# worker (the dispatch brief tells it these exact commands)
router.py done --report <report.md>
router.py ask "<question>" --options a,b
```

Workers run unattended. They may write only their worktree, the router state, and the report's directory: Codex uses `-s workspace-write -a never`; OpenCode allows every external directory, because its directory rules are tool prompts rather than a sandbox (shell writes bypass them) and git or pnpm would otherwise stop it constantly; Claude Code runs in `auto` mode with `--add-dir`. Claude Code asks for trust on every new path, worktrees included, so `dispatch --host claude` pre-trusts the worktree in `~/.claude.json`, but only when the user already trusts one of its parent directories (`trustedBy` in the output); otherwise the worker stops on the prompt and is reported `stalled`. Pin `--model` for OpenCode, because its default model can be a rate-limited free one. Each worker runs in its own tmux session, `wfc-<dispatch id>`, on the controller's tmux server: the watchdog treats a missing session as `exited`, so the controller and its workers must use the same tmux server and `TMUX_TMPDIR`. Use `tmux attach -t wfc-<id>` to watch it or step in, and leave with Ctrl-b d. Some terminals (VS Code's, for one) take Ctrl-b for themselves; then run `tmux detach-client -s wfc-<id>` from another shell. Ctrl-C would stop the worker; `resume` brings it back. The brief also asks the worker to stop any server or background process it started before it reports. State is kept in SQLite under `$WFC_STATE`, which defaults to `~/.local/state/workflow-core`. Run the tests with `python3 -m unittest discover -s tests`.

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
