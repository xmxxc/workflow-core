# workflow-core

A lightweight router between the controller and the workers it dispatches, for the `ai-coding` workflow maintained in `/path/to/skills-repo`. Workers report or ask through the router and then sleep. The controller sleeps on `router.py wait` and is woken by their messages.

## Status

Phase 1. `router.py` dispatches Codex, OpenCode and Claude Code workers to a Claude Code controller. Each host was verified end to end with a real worker (dispatch → ask → reply → done). The stalled-worker fallback and pre-trusting new worktrees come next. `notify_controller.py` holds the earlier hook-based classifier, which the fallback will reuse.

## Usage

```sh
# controller
router.py dispatch --host codex|opencode|claude --task <task.md> --cwd <worktree> --report <report.md> [--model <model>]
router.py wait --timeout 900        # blocks until a worker message arrives; exit code 3 on timeout
router.py reply <question-id> "<answer>"

# worker (the dispatch brief tells it these exact commands)
router.py done --report <report.md>
router.py ask "<question>" --options a,b
```

Workers run unattended. They may write only their worktree, the router state, and the report's directory: Codex uses `-s workspace-write -a never`; OpenCode allows exactly those external directories; Claude Code runs in `auto` mode with `--add-dir`. Pin `--model` for OpenCode, because its default model can be a rate-limited free one. Each worker runs in its own tmux session, `wfc-<dispatch id>`; use `tmux attach -t wfc-<id>` to watch it or step in. State is kept in SQLite under `$WFC_STATE`, which defaults to `~/.local/state/workflow-core`. Run the tests with `python3 -m unittest discover -s tests`.

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
