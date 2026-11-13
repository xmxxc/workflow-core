# workflow-core router-core-foundation-20261113

## Current state
- Scope: workflow-core core reliability tickets #2-#7 (issue mapping: task identity through backend contract)
- Development endpoint: each ticket has implementation, independent review, focused tests; stop before Windows/Herdr/native acceptance.
- Repository: /opt/workflow-core
- Branch: docs/router-core-spec-20261113
- Base: 22a3099 (wfc 0.4.5 baseline)
- WFC_STATE: /opt/workflow-core/.wfc-state/router-core-foundation-20261113
- wfc version: wfc 0.4.5
- Controller: delegated workflow-core controller session
- Role table: implementation Claude claude-opus-5-5 medium, fallback Codex gpt-6.1-sol medium; review Codex gpt-6.1-sol high with network
- Dependency order: #2 -> #3 -> #4 -> #5 -> #6 -> #7. Windows #8-#10 deferred until user testing.
- Current ticket: #2 prepared
- Active dispatches: none
- Last reconciliation: 2026-11-13

## Authorization
- User authorized implementation of workflow-core tickets #2-#7; merge, publish, Windows real acceptance remain user/live-verify steps.

## Ticket states
- #2 prepared; task identity and round/snapshot
- #3 pending; message ACK/replay, depends #2
- #4 pending; controller ownership/idempotent request, depends #3
- #5 pending; activity/fault signals, depends #2
- #6 pending; interrupt/safe handoff, depends #4/#5
- #7 pending; backend interface, depends #6

## History
- 2026-11-13: controller scope established; baseline and version recorded; no runtime changes yet.
