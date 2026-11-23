# Herdr kernel extraction: final implementation evidence, round 2

Dispatch `d-37371ace`, round-2 attempt `a-7423fe6d3bee`, issue #26,
2026-11-15 UTC. Round-1 attempt was `a-b1e6c39e2132`; its report and archived
artifacts remain historical evidence.
WFC starting commit `abbc399542ed28dce1c0877667733df9021387a0` on the supplied
`codex/herdr-kernel-feasibility` worktree. The initial working tree already
contained the controller's untracked feasibility directory. No tracked WFC
production file, PR25 artifact, tmux session or controller ledger was changed.

## Result and recommendation

The fixed PTY and Ghostty components **build and run independently on Linux**.
The release executable is **2,811,480 bytes (2.681 MiB)**; the supplied full
Herdr 0.9.5 Linux executable is **30,285,672 bytes (28.883 MiB)**, a 90.72%
reduction for these particular builds. This comparison is not a Windows size
estimate or an equal-feature performance benchmark.

The controlled host passes real PTY, Unicode argv/input, terminal interpretation,
resize, Ctrl-C, natural exit, ordinary descendant cleanup and identity fences.
A test-only backend runs the real WFC dispatch/round contract and a real child
WFC completion callback. An actual isolated OpenCode 1.19.5 TUI renders through
the extraction, accepts a Unicode draft, clears it on Ctrl-C and closes.

**Continue a bounded kernel investigation; do not take over production execution
yet.** The same experiment demonstrates three process-containment failures:
an escaped session survives close, a descendant survives after leader exit,
and a hangup-ignoring process survives server SIGKILL. Native Windows build,
ConPTY/job lifecycle and actual-host acceptance remain pending. Those are
feasibility costs, not successful checks. The private stdio protocol deliberately
reports recovery as lost and supplies no reconnect or input replay.

## Fixed source and extraction seam

Primary source is the local checkout `<pinned-herdr-checkout>`, Herdr 0.9.5 commit
`ee5bfb5041bb3e4a49d591c72308aae3c81ecdde`, Git tree
`d2fc8428a9ce5be7e1d89a10b62be3e289b479dd`. Ghostty's vendor base is
`44f2a44df7e8c4a0c6df3f7d872ef3d7ead88e51`, as recorded in the copied vendor
JSON. The component copies have **zero local upstream edits**.

Source reads included applicable AGENTS guidance, root Cargo.toml, both patch
indexes, Ghostty vendor JSON and build.rs, portable-pty's Unix and Windows
implementations, and Herdr's platform/process-group/Windows status-command
job paths. Source-map analysis is owned separately in
`herdr-kernel-source-map.md`; this experiment did not modify it.

| Retained component | Purpose and correctness boundary |
| --- | --- |
| `vendor/portable-pty` 0.9.0 | Actual PTY, argv/environment launch, resize, child status; retains all four Herdr patches |
| `crates/ghostty-vt` | Unmodified Rust wrapper, generated C bindings, terminal query replies and text formatter |
| `vendor/libghostty-vt` | Real Ghostty VT state, alternate/primary screen, cursor and Unicode handling; unmodified build graph |
| Vendor pins, patches and licenses | Exact attribution and patch maintenance provenance |
| Authored `src/main.rs` | Private stdio service, generation/unit nonce, literal input, viewport reads, direct lifecycle observation |
| Authored Python probes | Controlled workload, test-only WFC backend and actual OpenCode TUI exercise |

Herdr's root package, root build.rs, server, TUI, routing, layouts, remote
transport, detectors and persistence are absent from Cargo's package graph.
The extraction does not compile or invoke the full Herdr package. Its scratch
contains only the selected upstream subtrees plus authored prototype files.

This is **not yet a narrow source closure**. The selected Ghostty vendor
subtree retains app-runtime, renderer, font, development/test fixtures and
image paths. They are counted even where the library build does not compile
them. Rust png/base64 compile through the unmodified wrapper. Zig builds
Wuffs, Highway and zlib and generates Unicode data through uucode; translate-c
and its pinned Aro dependency are build tools. The unmodified build also
fetches Ghostty themes. Cargo tree does not account for those Zig/C sources.

Do not remove the patched executable/environment resolution, constrained
ConPTY DLL selection, VT state/query replies, fresh execution identity or
observable exit/EOF distinction when reducing this seam. Robust process
ownership must be added before accepting production close/recovery semantics.
Herdr's Windows `StatusCommandGuard` uses a suspended process and kill-on-close
job for status commands; it is not an already extracted PTY job solution.
The vendored ConPTY Child API terminates a process, with no owned job tree.

## Reproduction commands and observed exits

Run from the WFC worktree:

```bash
python3 experiments/herdr-kernel-prototype/prepare.py
python3 experiments/herdr-kernel-prototype/build_probe.py
python3 experiments/herdr-kernel-prototype/probe.py --actual-host
python3 experiments/herdr-kernel-prototype/metrics.py
```

Final preparation, locked build, authored Rust formatting check, Python syntax
check, full controlled/WFC/actual-TUI probe and metrics all exit **0**. The
preparer prints the exact commit, 1,378 copied files, 20,553,673 bytes and the
selected paths. Its `extraction.json` contains every original path and SHA256.
It rejects dirty selected paths, including untracked/ignored ones, before
copying. It refuses an existing unmarked scratch. A separate sparse temporary
clone supplied a modified LICENSE: exit **1**, `dirty selected source paths:
 M LICENSE`. A copied preparer with an intentionally wrong expected pin exits
**1**, `source commit differs from fixed Herdr pin`. The original preparer and
fixed checkout were unchanged by these fixtures. An initial fixture tried
`HEAD^` and exited 128 because the supplied checkout is shallow; the expected-pin
fixture replaces that unavailable parent check.

The exact successful build entry is:

```bash
env CARGO_HOME="$WFC_TOOLCHAIN_DIR/cargo" \
  RUSTUP_HOME="$WFC_TOOLCHAIN_DIR/rustup" \
  PATH="$WFC_TOOLCHAIN_DIR/cargo/bin:$PATH" \
  ZIG="$WFC_TOOLCHAIN_DIR/zig-x86_64-linux-0.16.0/zig" \
  ZIG_GLOBAL_CACHE_DIR="$WFC_TOOLCHAIN_DIR/zig-cache" \
  cargo +1.96.1 build --release --locked --offline \
    --manifest-path "$WFC_SCRATCH_DIR/Cargo.toml"
```

The first three manual builds exited **101**, at 8.64 s, 1.05 s and 9.11 s,
because Zig's native HTTP requests failed (`400 Bad Request` or
`HttpConnectionClosing`). Curl fetched the identical pinned archives and
`zig fetch <local archive>` verified the recorded content hashes. The bounded
build driver then had three dependency-fetch failures (exit 101) and its
fourth compile succeeded in **168.881 s**. This was the first successful
Ghostty compile, with some Rust dependencies and downloads already cached;
it is not a fully cold-cache total. Those failures and timings are historical
round-1 evidence. After round-2 rustfmt, the locked driver rebuild succeeds in
**6.039 s**, with dependency caches retained. The final separate locked/offline
check succeeds in **0.064 s** with warm caches. Full results and per-attempt
commands are retained in artifacts; round-1 final snapshots are in `artifacts/r1/`.

Final `build_probe.py` uses `--locked`; its first exploratory invocation predated
the prototype lock and used the upstream lock as a seed. The final preparer
copies the retained prototype lock. `cargo fetch --locked` exits 0 to obtain
the all-target metadata dependency sources; measurements then use `--offline`.
No global toolchain/package configuration was changed. In round 1, `cargo fmt`
exited 1 because rustfmt was absent. That failure remains in
`artifacts/r1/format-check.json`. The controller subsequently installed the
matching component in the isolated toolchain. Round 2 ran only:

```bash
env CARGO_HOME="$WFC_TOOLCHAIN_DIR/cargo" \
  RUSTUP_HOME="$WFC_TOOLCHAIN_DIR/rustup" \
  "$WFC_TOOLCHAIN_DIR/cargo/bin/rustfmt" +1.96.1 --edition 2021 \
    experiments/herdr-kernel-prototype/src/main.rs
env CARGO_HOME="$WFC_TOOLCHAIN_DIR/cargo" \
  RUSTUP_HOME="$WFC_TOOLCHAIN_DIR/rustup" \
  "$WFC_TOOLCHAIN_DIR/cargo/bin/rustfmt" +1.96.1 --edition 2021 --check \
    experiments/herdr-kernel-prototype/src/main.rs
PYTHONPYCACHEPREFIX="$WFC_SCRATCH_DIR/pycache-r2" \
  /usr/bin/python3 -m py_compile experiments/herdr-kernel-prototype/*.py
```

Both formatting operations and the Python check exit 0. The rustfmt component
reports `rustfmt 1.9.0-stable (31fca3adb2 2026-06-26)` for Rust 1.96.1. No
selected upstream Rust was formatted. The new extraction manifest is identical
to round 1, and all five Python source hashes are unchanged. The authored Rust
formatting diff is retained as `artifacts/authored-rust-format.diff`; no lifecycle
logic was added or containment gap repaired. Python bytecode stays in scratch.
The README now includes exact source-fetch/checkout and fixed Rust/Zig setup
instructions; preparation itself only verifies the supplied checkout, not
source download or tool bootstrap. This remains a private stdio extraction
feasibility prototype, not an independent production runtime.

Toolchain: Rust/Cargo 1.96.1 (`rustc 31fca3adb`, LLVM 22.1.2), Zig 0.16.0,
Python 3.12.3, x86_64 Linux GNU. Zig archive SHA256 matches controller metadata:
`70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00`.
Only `x86_64-unknown-linux-gnu` is installed as a Rust target. Rust profile is
release defaults with `strip=true`, thin LTO and one codegen unit; Ghostty uses
the unmodified build.rs defaults, `ReleaseFast`, SIMD on. `file` confirms a
stripped ELF PIE; `ldd` shows libc/libgcc and the dynamic loader. Ghostty is
statically linked. The generated 10,436,328-byte Ghostty `.so` is a build
byproduct and is not needed beside the executable; its static archive is
18,532,732 bytes, also not a shipped executable dependency.

## Controlled runtime evidence

The saved final `probe-results.json` has 22 records. Request/response ids,
generations, nonces, screens, exit facts and callback receipts are preserved.
All checks below ran against the release executable, using the real PTY and
Ghostty formatter rather than stripped stdout.

| Probe | Observed result | Limit |
| --- | --- | --- |
| Startup/argv | `space argument 中文`, `emoji 🐈` retained; stdin/stdout/stderr isatty all true | Linux host |
| Cursor overwrite | `wrong\rRIGHT` plus erase-to-EOL yields RIGHT, with wrong absent | Basic VT case |
| Literal input | Punctuation, shell-looking text, quotes, Chinese and emoji round-trip through controlled host | Child is Python, not a shell |
| Alternate screen | ALT_SCREEN on Alternate with START absent; main restore returns Primary with prior START | Real Ghostty state |
| Resize | PTY + parser change 100×30 to 120×35; child `os.get_terminal_size` reports 120×35 | No resize stress/load case |
| Ctrl-C | Input byte 0x03 invokes child's signal handler, displays CTRL_C_HANDLED | Default tty signal mode |
| Natural exit | `exit:7` produces code 7 and PTY EOF | Exit and EOF are separately observed |
| Ordinary close tree | Live leader, child and grandchild terminate on owned group kill | Zombies count as terminated; not an escaped group guarantee |
| Wrong nonce | Input, resize and close return wrong_nonce; live screen/size preserved | Generation checked first |
| Reused name | Old nonce cannot close a newly spawned unit with the same name | New nonce on every spawn |
| Restart fence | Old generation rejects input, close and resize against a new process; new control input works | Prior PTY reported lost |
| Unknown acknowledgement | Child writes APPLIED_ONCE; client deliberately discards input receipt, then kills server; marker remains once after restart | Caller must not retry unknown input; protocol has no same-generation dedup |
| Escape containment | setsid descendant survives close | **Observed failure**, explicitly test-killed |
| Parent-exit containment | Same-group hangup-ignoring descendant survives after parent exits and close | **Observed failure**, explicitly test-killed |
| Server crash containment | Hangup-ignoring child survives server SIGKILL | **Observed failure**, explicitly test-killed |

The normal controls and rejected actions are both recorded. Rejected wrong-nonce
resize leaves the original 100-column geometry; rejected close leaves the unit
running. After generation rejection, a fresh literal control reaches the new
host. Output events also carry the unit nonce so queued old output cannot
mutate a reused name's terminal.

The service uses one private stdio connection and in-memory state. No durable
PTY registry, reattach, restart handshake reconciliation, service-authentication
endpoint, cancellation budget or output backpressure exists. Acknowledged input
means bytes were written, not that an agent accepted a task. Stdin EOF performs
ordinary close; SIGKILL bypasses destructors. Unit.close deliberately avoids
signaling a potentially reused group after its leader has been reaped, exposing
the measured descendant gap rather than claiming safe cleanup.

## Actual WFC contract and completion callback

The candidate zipapp is `$WFC_CANDIDATE_DIR/wfc.pyz`, source
marker `d8e0695f91e6a21dd40f309596e131d0e3e95f58`, SHA256
`785e46dcdf11a8982fa1dd44a9e955941a9748245874d0ca2f37043094d1c55d`.
`probe.py` imports its real router and registers `KernelBackend` **only in the
probe process**. It replaces the host launcher with the controlled Python host
and calls real `router.cmd_dispatch`; WFC owns SQLite rounds and attempts.
Backend create starts the PTY, inspect/read observe it, submit sends a literal
Unicode line, interrupt produces CTRL_C_HANDLED, and close returns completed.
Its deliberately unsupported resume returns uncertain with a lost-PTY reason.

The controlled child writes its report and executes the real command:

```text
/usr/bin/python3 $WFC_CANDIDATE_DIR/wfc.pyz done
  --report $WFC_SCRATCH_DIR/controlled-wfc-1794745658903497774/report.md
  --attempt a-3a01bf9d4404 --message-id controlled:a-3a01bf9d4404
```

The final controlled fixture dispatch is `d-dcd9f390`, fixture round 1, attempt
`a-3a01bf9d4404`, scope `s-7617d75e`. The callback exits **0**; WFC stores a type=done message with
that attempt and request key and changes the round to **completed**. The
callback receipt remains `unsent` because the isolated fixture has no controller
relay. This demonstrates real callback/ledger behavior, not delivery to a live
controller or a full host-policy integration. No WFC identities, owner fencing,
message acknowledgement or wakeup policy were moved into the kernel.

## Actual OpenCode host

OpenCode **1.19.5** was launched in a real extracted PTY with XDG config/data/
state/cache directories under
`$WFC_SCRATCH_DIR/actual-host-1794745659341944003` and
`OPENCODE_CONFIG_CONTENT={"autoupdate":false}`. No existing user host/session
was reused. Startup's interpreted Alternate screen contains the OpenCode logo,
input prompt, working directory and version. Startup observation takes
**4.740 s**; the full draft/Ctrl-C/close exercise takes **5.202 s** on this run.

Typing `kernel isolated draft 中文` is visible in the actual prompt. Ctrl-C
clears it, returning the prompt placeholder; close terminates the host PID.
Identity is nonce `e15bc1a1-ab92-4553-ab0b-bad11c65e092`, process-local PID 34
in the probe's execution namespace. Screens and identity are saved verbatim.
This is an actual TUI input/render/interrupt/close probe, **not** an authenticated
model task or an actual-agent WFC completion callback. That larger acceptance
remains a later host integration task. The controlled host supplied the real
WFC callback evidence independently.

## Measured scope and provenance artifacts

Counts are physical nonblank source lines, including comments. Bytes include
all retained files. Inline-test counts are format-based subsets of their
category, not additional lines; dedicated fixture files are separate. These
are not claims about semantic production-only LOC.

| Category | Files | Bytes | Nonblank source lines | Inline-test subset |
| --- | ---: | ---: | ---: | ---: |
| Rust Ghostty retained | 6 | 227,091 | 5,972 | 1,205 |
| Generated Rust bindings | 1 | 465,602 | 5,375 | 0 |
| Dedicated Rust tests | 3 | 15,402 | 412 | 0 |
| Ghostty Zig/vendor retained | 1,277 | 19,317,473 | 414,513 | 102,993 |
| Ghostty test/fixture files | 68 | 363,699 | 0 in counted code extensions | 0 |
| Patched portable-pty vendor | 19 | 137,040 | 2,852 | 171 |
| Root/provenance/license files | 4 | 27,366 | 0 | 0 |
| **Selected total** | **1,378** | **20,553,673** | **429,124** | **104,369** |

Authored prototype source is **841 nonblank lines**: service Rust 277;
prepare 65; build helper 55; controlled host 38; probe/adapter 335; metrics 71.
Cargo manifests, lock, documentation and evidence artifacts are excluded from
that authored code count. The complete Herdr main `src/**/*.rs` baseline is
402 files, 10,465,617 bytes, 291,081 physical lines or 269,910 nonblank lines,
including tests. Comparing that main-Rust count with a retained Zig/vendor
count would be misleading; both are stated with their boundaries.

`cargo tree -e normal,build` has **41 unique packages including the root** on
Linux: 40 dependencies, two local and 38 registry packages. The all-target
normal/build tree has **61 including root**. Unfiltered cargo metadata lists
**87 packages**, also representing declared target/feature dependency records;
it does not mean 87 packages compiled on Linux. Full versioned closures and
metadata package licenses are saved in metrics/tree artifacts. WFC policy and
full Herdr packages do not appear in these closures.

Executable SHA256:
`ead8b312c079324d28ecb6cb4586b3548cfd378e42a6120c97d7496e27fabe3c`.
Formatted authored Rust SHA256:
`6c8b0a2910575734a034ada255e3a91391ea714238757552e7f15eb7f21268c9`.
Full Herdr SHA256:
`14d9e8bc8b4a9e0118d77c32266f76d461f09b40342eb23752960bd89f59929d`.

`experiments/herdr-kernel-prototype/artifacts/` retains copied-file hashes,
source categories, Rust versions/closure/licenses, terminal observations,
callback identity/receipt, build failures/success/times and the generated-file
inventory (66 files across installed Ghostty outputs and cached generated Zig
sources). Build caches, Unicode tables, generated headers/pkg-config files,
static/shared libraries and target outputs stay in scratch. `bindings.rs` is
upstream-generated with rust-bindgen 0.72.1 and is retained verbatim. Prototype
Cargo.lock is retained in the authored directory.

Four portable-pty patches remain: 0001 ConPTY loading, 0002 raw command tails,
0003 malformed environment rejection, 0004 directory-skipping executable
resolution. Six Ghostty patches remain: 0002 modifyOtherKeys query; 0004 hosted
Wuffs build; 0005 bounded word selection; 0006 cursor-line-preserving clear;
0007 experimental PNG/source retention; 0008 cursor hyperlink page replacement.
Original indexes include rationale/removal conditions and are copied with patch
files. Apache-2.0 Herdr/Rust binding, MIT portable-pty/Ghostty and the retained
AFL++ development license paths/hashes are preserved. Fetched dependency
notices remain with Cargo/Zig sources; a future redistributable would need its
own notice/package pass. No upstream issue or contribution was made.

## Platform and acceptance mapping

Native Windows and cross compilation are **unmeasured**. This machine is Linux,
the installed Rust target is Linux only, there is no Windows VM/runner in the
brief and SDK license acceptance is excluded. No SDK was fetched, no Windows
binary exists and no Windows size is extrapolated. Exact native Developer
PowerShell build commands and controlled/actual-host reproduction checks are
in the prototype README. Native ConPTY Ctrl-C, Windows argv/environment, job
assignment/descendant cleanup, process handles and app-local ConPTY loading
must be measured there. The Linux helper uses `/proc`, Unix env and pipe
select; it is expressly not a native Windows harness.

| Issue #26 acceptance | Final prototype result | Outstanding boundary |
| --- | --- | --- |
| Reviewable seam/dependencies/maintenance | Implemented, local artifacts ready | Independent controller review pending |
| Source inventory/pins/hashes/patches/licenses | Implemented, verified selected copies | Distribution notice/package work deferred |
| Independent build and measured size | Linux passed; source/build baggage counted | Windows unmeasured |
| WFC create/observe/input/callback/interrupt/close | Controlled host passed real callback | Containment failures; no reconnect/recovery integration |
| Identity isolation/unknown input/restart | Negative controls and lost-ack marker passed | Same-generation dedup stays upper-layer responsibility |
| Linux/native Windows results | Linux controlled and actual TUI evidence | Native Windows explicitly pending |
| Actual host + other-platform reproduction | Actual OpenCode TUI input/interrupt/close; Windows steps supplied | Authenticated host task+done still pending |
| Extracted versus fixed full Herdr comparison | Measured Linux bytes, maintenance/install tradeoff below | No performance/platform parity assertion |
| Independent review/recommendation/tickets | Evidence and drafts supplied | Controller owns review, final decision and issue publication |

The full issue is not claimed complete. This worker completed the assigned
prototype/evidence deliverables and identified the pending acceptance items.

## Fixed full Herdr versus independent extraction

| Dimension | Independent extraction | Fixed complete Herdr |
| --- | --- | --- |
| Measured Linux executable | 2.811 MB decimal | 30.286 MB decimal |
| User install | A packaged companion could avoid end-user Rust/Zig; packaging not built | Existing full artifact can be pinned/distributed; artifact provenance still required |
| Host setup | Host executable, path and authentication still required | Same host responsibilities remain |
| Local execution | Small private NDJSON with lost recovery | Rich existing API/server/session implementation |
| Source maintenance | Own service + 20.55 MB retained source/build graph + Rust/Zig patch tracking | Existing upstream runtime correctness, larger surface and version-specific adapter contract |
| Lifecycle confidence | Linux primitive evidence; demonstrated containment gaps | This experiment did not revalidate the full runtime; PR25/#9/#10 remain separate |
| Windows | Direct-child branch only, no build or runtime evidence | Existing Windows packaging does not replace native acceptance |

Binary reduction is real. Source/dependency reduction needs another deliberate
extraction step, and the lifecycle work is a substantial part of ownership.
Keep fixed full Herdr as the comparison baseline until the narrow kernel has
bounded cleanup and recovery semantics on both executable platforms.

## Draft dependent work for controller review

1. **Specify lifecycle outcomes and loss handling.** Define generation/unit
   ownership, bounded close, process-exit versus output-EOF, uncertain input,
   no implicit replay and WFC reconciliation. Characterize the three observed
   containment failures before changing behavior.
2. **Implement process ownership beneath the agreed contract.** Depends on 1.
   Select Linux escaped-descendant/crash containment and native Windows
   suspended-launch/job assignment. Preserve process identity across observation
   and reaping. Require controlled adversarial cleanup evidence before backend
   integration.
3. **Narrow the unmodified build/source seam.** Depends on this baseline and 1.
   Remove themes/UI/font/development source and unnecessary image build baggage
   only with VT/Unicode/alternate-screen characterization intact; measure
   source, generated artifacts, compiled graphs and binary size separately.
4. **Native Windows and authenticated-host acceptance.** Depends on 1/2 and an
   already licensed runner/toolchain. Record actual ConPTY mode, direct/native
   executable paths, DLL provenance, Ctrl-C, resize, descendant cleanup, crash
   cases and package bytes; run an isolated real host task with a WFC callback.
5. **Evaluate a production WFC adapter candidate.** Depends on successful 2–4
   and independent review. Keep dispatch/attempt/owner/message/wakeup in WFC;
   compare user installation and fault handling against the fixed complete
   Herdr baseline. Production enablement/migration remains a separate decision.

All test-created servers and background hosts are stopped. The final cleanup
record confirms all deliberately owned controlled PIDs terminated; the actual
OpenCode PID terminated on close, and every build/probe session returned.
Scratch and text artifacts remain for review, with no running experiment service.
