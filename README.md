# workflow-core

Wakes a sleeping controller window when a worker or reviewer window finishes, for the `ai-coding` workflow maintained in `/path/to/skills-repo`.

## Status

Phase 1, completion wake. The host probes listed in the handoff have not been run yet, so no code exists.

## Scope by phase

1. **Completion wake**: a finished worker or reviewer wakes the controller window through its host's own entry point. Neither a resident scheduler nor a headless controller is required.
2. Different hosts for different roles: GPT runs in Codex CLI, Claude in Claude Code, and other models in OpenCode.
3. Independent run: the controller scans `ready-for-agent` issues, in observe mode first.
4. Lighter controller: mechanical work moves into code.
5. More wake sources: Hermes gateway, MCP and a panel.

Each phase adds to the previous one, and each phase can be used on its own.

## Documents

- [Handoff, 2026-11-04](docs/handoff-20261104-completion-wake.md): decisions made so far, the host wake matrix, the probes to run first, and the directions that were rejected.
