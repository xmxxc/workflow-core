# #26 · r1 acceptance addendum

Formal issue now exists: https://github.com/xmxxc/workflow-core/issues/26 . The controller fetched its body to sibling `issue-26-source.md`. This is the same scope, not a replacement implementation or separate ticket. Preserve your existing work; read the acceptance criteria once and include a criterion/result/limitation mapping in the report.

Two acceptance requirements need explicit evidence:
- Execute the prototype through a WFC execution contract with a controlled host including an actual WFC completion callback; direct NDJSON probes alone are not the complete criterion. Keep any adapter/harness inside experiments/, without modifying router.py or production backends. A small test-only ExecutionBackend seam or controlled WFC callback harness is acceptable; distinguish exactly what ran.
- At least one actual Codex/OpenCode host probe on an executable platform is required (not merely optional). Use isolated state/config/artifacts as necessary; do not reuse user's active sessions. If blocked after concrete attempts, preserve failure diagnosis and mark that acceptance item pending; don't claim scope complete. Provide native Windows reproduction steps even if no Windows build/runtime available.

Formal delivery includes source hashes, patch/license provenance, full-vs-extracted comparison, continue/stop recommendation and draft dependent tickets. Controller owns final synthesis/tickets and independent review. Your report should supply measured inputs, not overstate cross-platform or crash recovery.

No new scope ticket; no automatic production migration/tmux removal, merge or release. PR25 remains independent.
