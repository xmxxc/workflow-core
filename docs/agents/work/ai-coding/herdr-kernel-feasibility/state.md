# Herdr execution kernel feasibility

## Current

- Formal issue: [#26](https://github.com/xmxxc/workflow-core/issues/26), source snapshot `prompts/issue-26-source.md`; acceptance additions sent in `prompts/kernel-impl-r1-addendum.md`. Do not create duplicate scope issues.

- User-authorized source extraction feasibility research and isolated runnable prototype; no production backend integration, formal takeover, tmux removal, session migration, merge or release.
- Research branch `codex/herdr-kernel-feasibility`, base `abbc399542ed28dce1c0877667733df9021387a0`; worktree `<research-checkout>`. PR25 remains branch `codex/opencode-2x-windows`, head d8e0695, CI8/8 passed, artifact14159825535; new candidate Windows actual run still pending user audit/retest.
- Fixed Herdr0.9.5 source `<pinned-herdr-checkout>`, commit `ee5bfb5041bb3e4a49d591c72308aae3c81ecdde`. No edits or upstream publication.
- Workflow: `super-coding/skills/super-coding/SKILL.md` read; existing roles `docs/agents/roles.md`: Codex `gpt-6.1-sol` / high, separate implementation/review WFC dispatch. Research skill background source investigation is supplemental, not an independent review.
- WFC command `python3 <candidate-dir>/wfc.pyz`, version0.4.7-rc.1 (fixed d8e0695 payload); state `<private-state>`.
- Controller thread `01a1dd4c-05e6-7942-bd2d-2ce16cbc2836`; unique relay pid1592555, log `<state>/relay.log`, no parallel wait. Started from controller environment; first real delivery into this chat confirmed by receipt of exited message m-50728b8621a948c281517cd88745d478. Do not touch any other range's relay.
- Minimal durable notes only; no new factory logging infrastructure.

## Tasks

| Task | Identity / path | State | Next |
|---|---|---|---|
| Source audit | research subagent `herdr_kernel_sources`, `docs/research/herdr-kernel-source-map.md` | returned: source map read, source/patch references checked; no runtime claim | Use as experiment/review source |
| Prototype implementation | WFC d-37371ace, attempt a-b1e6c39e2132, model gpt-6.1-sol/high | r2 returned; final source/binary hashes and artifacts checked | Freeze candidate and dispatch independent review |
| Failed initial startup | WFC d-1c2107ee, attempt a-d7483847f47c | blocked: process exited1 before work (TERM=dumb) | Retain diagnostic; corrected isolated launch env for replacement, no unknown-input replay |
| Independent review | not dispatched | prepared later | Distinct WFC session on fixed candidate after implementation and source map |
| Recommendation / spec / dependent ticket drafts | controller | pending evidence | User decides formal ownership/migration scope |

Implementation task: `prompts/kernel-impl-r1.md`; report `reports/impl/r1.md`. Worker owns `experiments/herdr-kernel-prototype/` and `docs/research/herdr-kernel-prototype-evidence.md`; controller owns this ledger; research agent owns source map. No shared production files.

## Authorization / inputs

- User direction delivered by operator-b5805dc4a72645d2996d3eaebcc606b7 in this chat; not a router DB message, no ack issued for that instruction.
- Explicitly authorized source analysis, isolated build/run, controlled-host validation then real-host probe, preserve correctness mechanisms and report limits; prepare reviewable docs/prototype/spec/ticket proposals. No formal migration approved.
- Worker network exception: fetch pinned Cargo/Zig dependencies needed for independent build; no credentials/global installation/SDK license acceptance. Controller installed isolated Rust1.96.1 (official rustup-init checksum verified) and Zig0.16.0 (official download index SHA256 verified) under `<toolchain-dir>`; no global PATH/profile change.
- Scratch `<scratch-dir>`; tool inputs/source manifests under `<toolchain-dir>`. Do not clean up active artifacts or worker sessions without user direction.

## History

- 2026-11-15: New branch from clean release base; PR25 candidate left unchanged. Source audit found PaneRuntime couples UI/layout/detect; extraction experiment starts at patched portable-pty and Ghostty crate. Windows PTY/tree and Unix-only handoff remain explicit risks, not passed claims.
- 2026-11-15: Scoped relay `--detach` with scope but no owner token raises TypeError in existing payload. Workaround: unique-state unscoped relay; no router changes in this scope. Initial worker TERM=dumb exited; process/receipt inspected before explicit replacement launch with TERM=xterm-256color. Replacement confirmed process started, backend online.

- 2026-11-15: Source research returned. Source map read and checked; 4 portable-pty patches and 6 Ghostty patches recorded, Windows pane tree vs status Job guard distinction and Unix-only live-PTY handoff explicit. Implementation terminal confirmed accepted/running with source reads; await WFC callback and fixed evidence.

- 2026-11-15: User-authorized project direction assigned formal issue #26; acceptance fetched and reconciled with active r1. Required controlled WFC completion callback and at least one real-host probe made explicit in additive task note. #9/#10 remain platform/backend acceptance; PR25 unchanged.

- 2026-11-15: #26 kickoff milestone published https://github.com/xmxxc/workflow-core/issues/26#issuecomment-6893960937 . Active implementation received queued nudge message2 with source snapshot/addendum; terminal shows the exact note. Implementation continues in same dispatch, no duplicate work. Operator notice itself was not acked.

- 2026-11-15: Relay delivered initial failed dispatch exited message m-50728b8621a948c281517cd88745d478 to this controller chat. Reconciled launch receipt exit1 TERM=dumb and active replacement d-37371ace before ack; message confirmed processed. Replacement remains online/working, no duplicate implementation or extra relay launched. No review task ready before prototype report.

- 2026-11-15: r1 done message m-7769ee2b759242019cf8aac446effc03 read with report and artifacts, ack processed statusVersion4. Linux standalone extraction measured 2,811,480 bytes vs full30,285,672; controlled callback and real OpenCode TUI observed, three adversarial cleanup gaps retained. No production readiness claim. Isolated rustfmt installed; same worker continued with prompts/kernel-impl-r2.md, report reports/impl/r2.md, to finish formatting and refresh fixed final evidence. Controller authored decision/spec/dependent-ticket drafts in docs/research/herdr-kernel-decision.md; no follow-up issues created.

- 2026-11-15: First prototype outcome milestone posted to #26 (numbers, observed containment failures, partial Windows/authenticated-host limits, r2/final-review next). Final candidate not yet frozen; r2 may change authored metrics/hash.

- 2026-11-15: r2 stalled message m-fa8454f1780649f59a855cbd4c9592c1 reconciled: host online, current round2/attempta-7423fe6d3bee, r2 report absent, stable composer held `[Pasted Content 1024 chars]` after r1 completion. Under dispatch input lock, rechecked backend ownership/liveness/current attempt and two unchanged captures; sent exactly one Enter without retyping brief. Stalled notification ack processed; activity confirmed: worker explicitly accepted r2 and read kernel-impl-r2.md in terminal. No new implementation dispatch, no WFC code changes in #26.

- 2026-11-15: r2 report returned (message m-6c4ce47f51344181a312d4bc86299109), read and ack processed. Authored-only rustfmt, Python syntax, locked/offline build and 22 probe records passed implemented checks; three known containment gaps retained. Final ELF2,811,480 bytes sha256ead8b312c079324d28ecb6cb4586b3548cfd378e42a6120c97d7496e27fabe3c; authored841 nonblank lines. Fixed-source extraction and actual TUI remain Linux-only, partial WFC/runtime acceptance explicit. Next candidate commit and separate WFC review; no production files changed.
