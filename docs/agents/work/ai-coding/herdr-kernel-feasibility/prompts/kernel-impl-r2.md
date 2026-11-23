# #26 · prototype hygiene and final evidence · round 2

Continue existing prototype; do not restart research or broaden into production fixes. Controller read r1 report/evidence and accepts its honest feasibility limits. The fixed source and ownership rules from r1 remain. Formal source: sibling issue-26-source.md.

Controller has now installed rustfmt in the isolated Rust1.96.1 toolchain (`$WFC_TOOLCHAIN_DIR`); initial missing-formatter failure remains historical evidence, not a final passed check.

Required:
1. Format ONLY authored Rust (`experiments/herdr-kernel-prototype/src/main.rs`) using rustfmt1.96.1, without formatting/modifying any selected upstream source. Keep upstream hashes unchanged. Do not expand lifecycle features or repair the three known containment gaps in this round; they are important findings for the ownership decision.
2. Re-materialize from fixed clean source, locked build, authored formatting check, Python syntax check and full prototype controlled/WFC callback/actual OpenCode TUI probes on the resulting authored source. Update fixed evidence artifacts, metrics, executable/source hashes and report with actual final values and exits; keep initial failed build/formatter evidence identifiable as historical. Use temporary pycache or keep generated caches outside repo. Stop only test-owned processes.
3. Clarify reproducibility notes: prepare only verifies exact Git HEAD/selected clean paths; ensure experiment instructions say how to obtain/check out that fixed source and Rust/Zig tools (fixed versions). This is a private stdio extraction feasibility prototype, not an independent production runtime. Native Windows unmeasured; include exact build steps and point out Linux harness cannot run natively there. No need to fabricate Windows evidence.
4. Ensure evidence wording does not claim authenticated real-agent task/callback from the actual TUI draft probe, or production cleanup/recovery from the controlled partial successes. The larger criterion is partial where gaps exist. Preserve all known failures.

Paths same as r1: own experiments/herdr-kernel-prototype/ and docs/research/herdr-kernel-prototype-evidence.md; controller ledger/source map untouched. No commit/push/PR/global install/upstream edits. Report r2.md in same supplied report directory, include changes from r1, exact current files and final evidence. Controller will synthesize recommendation/draft ticket spec and dispatch separate independent review after you return.
