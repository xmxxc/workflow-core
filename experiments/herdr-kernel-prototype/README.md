# Throwaway Herdr kernel extraction

Question: can fixed Herdr 0.9.5 PTY and VT components run independently as a
small WFC companion? Linux says yes for the primitive extraction; process
containment and native Windows acceptance remain unfinished. This directory
is a private stdio extraction feasibility experiment. It is not an independent
production runtime and has no production backend selection.

## Obtain the fixed source and tools

The controller-supplied environment already has these inputs. For a fresh
Linux reproduction, use new disposable directories. Preparation does not
download source or install tools; it verifies the exact Git HEAD and selected
clean paths of the checkout supplied to it. To obtain that checkout:

```bash
git init /tmp/wfc-herdr-source
git -C /tmp/wfc-herdr-source remote add origin https://github.com/herdrdev/herdr.git
git -C /tmp/wfc-herdr-source fetch --depth=1 origin ee5bfb5041bb3e4a49d591c72308aae3c81ecdde
git -C /tmp/wfc-herdr-source checkout --detach ee5bfb5041bb3e4a49d591c72308aae3c81ecdde
git -C /tmp/wfc-herdr-source rev-parse HEAD
git -C /tmp/wfc-herdr-source status --porcelain=v1
```

Obtain Rust through the official rustup bootstrap into private tool directories,
selecting compiler **1.96.1** and its rustfmt component. The bootstrap installer
is not a vendored experiment dependency; the compiler version is fixed:

```bash
mkdir -p /tmp/wfc-kernel-tools
curl --proto '=https' --tlsv1.2 -fL https://sh.rustup.rs \
  -o /tmp/wfc-kernel-tools/rustup-init.sh
env CARGO_HOME=/tmp/wfc-kernel-tools/cargo \
  RUSTUP_HOME=/tmp/wfc-kernel-tools/rustup \
  sh /tmp/wfc-kernel-tools/rustup-init.sh -y --no-modify-path \
    --profile minimal --default-toolchain 1.96.1 --component rustfmt
```

Obtain the fixed Linux Zig archive from its official release source and verify
the supplied release hash before extracting:

```bash
curl -fL https://ziglang.org/download/0.16.0/zig-x86_64-linux-0.16.0.tar.xz \
  -o /tmp/wfc-kernel-tools/zig.tar.xz
printf '%s  %s\n' \
  70e49664a74374b48b51e6f3fdfbf437f6395d42509050588bd49abe52ba3d00 \
  /tmp/wfc-kernel-tools/zig.tar.xz | sha256sum -c -
tar -xJf /tmp/wfc-kernel-tools/zig.tar.xz -C /tmp/wfc-kernel-tools
env CARGO_HOME=/tmp/wfc-kernel-tools/cargo \
  RUSTUP_HOME=/tmp/wfc-kernel-tools/rustup \
  /tmp/wfc-kernel-tools/cargo/bin/rustc +1.96.1 --version
/tmp/wfc-kernel-tools/zig-x86_64-linux-0.16.0/zig version
```

These are fresh-environment instructions, not additional installations performed
in round 2. The probe additionally needs a native installed OpenCode executable
on PATH and the WFC candidate zipapp (default path in `probe.py`, overridable
with `--wfc`). The measured host was OpenCode 1.19.5. Source/tool setup and
controlled/actual-host dependencies are separate from the service binary.

## Reproduce on the supplied Linux environment

```bash
python3 experiments/herdr-kernel-prototype/prepare.py
python3 experiments/herdr-kernel-prototype/build_probe.py
env CARGO_HOME=/tmp/wfc-kernel-tools/cargo \
  RUSTUP_HOME=/tmp/wfc-kernel-tools/rustup \
  /tmp/wfc-kernel-tools/cargo/bin/rustfmt +1.96.1 --edition 2021 --check \
    experiments/herdr-kernel-prototype/src/main.rs
PYTHONPYCACHEPREFIX=/tmp/wfc-herdr-kernel-prototype-r1/pycache \
  python3 -m py_compile experiments/herdr-kernel-prototype/*.py
python3 experiments/herdr-kernel-prototype/probe.py --actual-host
python3 experiments/herdr-kernel-prototype/metrics.py
```

Preparation verifies Herdr commit
`ee5bfb5041bb3e4a49d591c72308aae3c81ecdde`, rejects modified or untracked/ignored
selected paths, and materializes tracked files under
`/tmp/wfc-herdr-kernel-prototype-r1`. It copies only `crates/ghostty-vt`, the two
vendor directories, their provenance/patch files, and the upstream license.
`extraction.json` enumerates every copied path and SHA256. Third-party source
is kept outside this repository. Source and scratch overrides are supported
by `prepare.py`; the measurement helpers target the named experiment scratch.

The build uses controller-supplied Rust 1.96.1 and Zig 0.16.0 under
`/tmp/wfc-kernel-tools`, with dedicated Cargo, Rustup and Zig caches. Its bounded
curl fallback downloads only URLs with hashes in selected `build.zig.zon`
files and compares Zig's computed content hash with that pin. It does not
edit upstream source. The retained `Cargo.lock` fixes the Rust resolution.
After dependencies have been fetched, the exact direct build is:

```bash
env CARGO_HOME=/tmp/wfc-kernel-tools/cargo \
  RUSTUP_HOME=/tmp/wfc-kernel-tools/rustup \
  PATH=/tmp/wfc-kernel-tools/cargo/bin:$PATH \
  ZIG=/tmp/wfc-kernel-tools/zig-x86_64-linux-0.16.0/zig \
  ZIG_GLOBAL_CACHE_DIR=/tmp/wfc-kernel-tools/zig-cache \
  cargo +1.96.1 build --release --locked --offline \
    --manifest-path /tmp/wfc-herdr-kernel-prototype-r1/Cargo.toml
```

Run `.../target/release/herdr-kernel-prototype` to start the service. Stdin EOF
closes the instance's remaining live units. Every line on stdout is JSON.
The probe harness closes its servers and explicitly kills its own deliberate
containment survivors; it never enumerates or terminates user sessions.

## Private NDJSON protocol

Startup emits `{"event":"hello","generation":"<uuid>","recovery":"lost",...}`.
All requests carry `id`, `generation`, `unit`, and `op`. Spawn requires an argv
array, `cols`, `rows`, and optionally `cwd`; it returns a fresh nonce and PID.
Other operations require that nonce. Unit names may be reused only after close.

```json
{"id":1,"generation":"<hello generation>","unit":"example","op":"spawn","argv":["/bin/sh"],"cols":80,"rows":24}
{"id":2,"generation":"<hello generation>","unit":"example","nonce":"<spawn nonce>","op":"input","text":"printf 'hello\\n'\r"}
{"id":3,"generation":"<hello generation>","unit":"example","nonce":"<spawn nonce>","op":"read"}
{"id":4,"generation":"<hello generation>","unit":"example","nonce":"<spawn nonce>","op":"resize","cols":100,"rows":30}
{"id":5,"generation":"<hello generation>","unit":"example","nonce":"<spawn nonce>","op":"close"}
```

`input` writes literal UTF-8 bytes, including `\u0003` for Ctrl-C. Shell
interpretation is determined by the child argv. `read` returns Ghostty's
interpreted viewport, active screen, size, exit status and PTY EOF. It can lag
new output; callers poll. Terminal query replies are written back to the PTY.
There is no output replay, request deduplication, reconnect, disk state, PTY
reattachment or implicit retry. A lost input acknowledgement is uncertain.
Restart generates a new generation and reports prior PTYs lost. WFC keeps
dispatch, attempts, messages, owners and wakeup policy.

Current kernel flow control: 64 event slots bound copied PTY output backlog
to 512 KiB, and one writer thread consumes a bounded 32-item FIFO for atomic
input/query responses. Input is limited to 2 MiB; acknowledgement follows the
completed write while terminal parsing continues. Queue saturation/write failure
is explicit. Standalone close still kills the verified live Unix leader's process group.
New sessions/groups, surviving descendants after leader exit, and children
ignoring hangup after server SIGKILL are **observed containment failures**.
These are historical standalone-prototype containment failures. The opt-in WFC
runtime supplies the separately documented cgroup/Job ownership boundary; see
`../../docs/bundled-backend.md`. The standalone Windows close operation still calls
the vendored direct-child kill operation. The new WFC Job lifecycle requires its
own native acceptance; the raw-stdio Windows baseline does not validate it.

## WFC and actual host evidence

`probe.py` imports the supplied candidate zipapp's actual `router` and registers
a test-only `ExecutionBackend` in that process. Real `cmd_dispatch` creates the
round; PTY input drives `controlled_host.py`; that child invokes the real
`wfc.pyz done --attempt ... --message-id ...`. The harness checks the resulting
SQLite done message and completed round. No backend is installed into WFC.

The separate actual-host probe starts OpenCode with isolated XDG config,
data, state and cache. It observes the real alternate screen, types an
unsubmitted Unicode draft, observes it, sends Ctrl-C and closes the unit.
It performs no model task. Its results are narrower than an authenticated
agent completion flow. Saved evidence is in `artifacts/probe-results.json`.

## Native Windows reproduction — pending, not a measured result

Use a disposable native Windows directory with Python 3, Git, Rust 1.96.1,
Zig 0.16.0 and an already licensed MSVC/Windows SDK toolchain. Do not accept
an SDK license as part of this experiment. Clone/check out the exact upstream
pin and copy this authored directory separately. In a Developer PowerShell:

```powershell
git init C:\Temp\herdr-0.9.5
git -C C:\Temp\herdr-0.9.5 remote add origin https://github.com/herdrdev/herdr.git
git -C C:\Temp\herdr-0.9.5 fetch --depth=1 origin ee5bfb5041bb3e4a49d591c72308aae3c81ecdde
git -C C:\Temp\herdr-0.9.5 checkout --detach ee5bfb5041bb3e4a49d591c72308aae3c81ecdde
$env:CARGO_HOME = 'C:\Temp\kernel-tools\cargo'
$env:RUSTUP_HOME = 'C:\Temp\kernel-tools\rustup'
$env:ZIG = 'C:\Temp\zig-0.16.0\zig.exe'
$env:Path = "C:\Temp\kernel-tools\cargo\bin;$env:Path"
rustup toolchain install 1.96.1 --profile minimal --component rustfmt
python .\experiments\herdr-kernel-prototype\prepare.py `
  --source C:\Temp\herdr-0.9.5 --scratch C:\Temp\herdr-kernel-probe
cargo +1.96.1 build --release --locked `
  --manifest-path C:\Temp\herdr-kernel-probe\Cargo.toml
Get-Item C:\Temp\herdr-kernel-probe\target\release\herdr-kernel-prototype.exe |
  Select-Object Length
Get-FileHash C:\Temp\herdr-kernel-probe\target\release\herdr-kernel-prototype.exe
```

The Windows commands assume an official rustup bootstrap already installed in
the specified private Cargo directory. Obtain the native Zig **0.16.0** Windows
archive from `https://ziglang.org/download/` and verify that release's published
checksum before extracting to the stated ZIG path. Use the already licensed
MSVC/SDK environment. These native steps were not executed here; the Linux
archive/checksum above does not apply to Windows.

Use redirected stdin/stdout of that executable, read its hello, and send the
same protocol above, using the absolute native Python executable followed by
`controlled_host.py`, a report path with spaces/Chinese, and the native WFC
zipapp path as argv. Verify `START` has all three isatty values true, overwrite
shows RIGHT, `alt`/`main` switch screens, resize+`size` observes new geometry,
literal Unicode input round-trips, Ctrl-C is handled, `exit:7` reports 7, and
bad nonce/generation input/close leave a fresh process live. Repeat the lost
acknowledgement probe without replay. Use an isolated native WFC backend
harness equivalent to `wfc_contract`; the Linux harness uses `/usr/bin/env`,
`select` on pipes and `/proc` and must not be called a Windows test.

Then launch a native installed OpenCode/Codex **executable** with isolated
configuration/state under the same PTY. Capture its startup screen, a Unicode
draft, Ctrl-C and close. Record SDK/build versions, ConPTY selection, exit
codes, actual executable/package bytes and descendant process identities.
Use only the test PIDs when checking/cleaning descendants. Explicitly test
grandchildren, an exited parent and server termination; direct-child close
is expected to expose a job-ownership gap. With no bundled `conpty/`, this
patched vendor defaults to system ConPTY. App-local bundle/hash/loading
acceptance is a separate pending case. No Linux size is a Windows estimate.

## Provenance and maintenance boundary

There are no local edits to the selected upstream components. The extraction
retains four Herdr portable-pty patches (ConPTY loading, raw command tails,
environment validation, executable resolution) and six Ghostty patches
(`0002`, `0004`–`0008`, including `0007`). Original patch indexes and patch files
are copied verbatim. Herdr/Rust binding use Apache-2.0, portable-pty and Ghostty
use their included MIT licenses; the selected AFL++ development directory has
its own included license. `artifacts/licenses.json` records original paths
and hashes. Preserve those notices and the fetched dependency notices in any
later distribution; this experiment does not prepare a distribution package.

`Cargo.lock`, generated Rust bindings and generated Zig tables have separate
provenance. `artifacts/metrics.json` gives physical nonblank lines (comments
included), file bytes, dedicated test/fixture categories and a format-based
inline-test subset. It is not a semantic production-only SLOC count.
`cargo tree` measures Rust dependencies, not the much larger retained source
tree or Zig's C dependencies. The unmodified vendor still contains rendering,
font, app-runtime, development and image code; Rust png/base64 and Zig
Wuffs/Highway/zlib remain build baggage. The build even fetches theme data.
The final executable statically links Ghostty and dynamically needs host
libc/libgcc; its sibling generated Ghostty shared object is not required.

The measured conclusion, acceptance mapping and dependent work are in
`docs/research/herdr-kernel-prototype-evidence.md`.

### Linux co-distribution trial with WFC rc.4

From this experiment directory, after the locked release build described above,
run `python3 bundle_linux.py PREPARED_TREE /path/to/wfc.pyz trial.tar.gz`.
Extract the archive in a fresh directory and run
`python3 wfc-kernel-trial/smoke.py`. The smoke uses the adjacent kernel and
zipapp. It injects the test backend into one router process, uses a controlled
Python worker, verifies its real WFC done callback and removes its temporary state.
The standard CLI has no kernel backend registration. Separate CLI invocations
would require a reconnectable service with persisted ownership.

On 2026-11-18, Linux x86_64, the rc.4 zipapp and the locked offline kernel
built from the PR #27 prototype passed after extraction with a fresh HOME,
a PATH containing only python3, and no Herdr endpoint. Dispatch d-876874e7
completed, including Unicode input, interrupt, done receipt and normal close.
The kernel was 2,811,480 bytes, and the trial archive was 1,330,783 bytes.
The kernel SHA-256 was ead8b312c079324d28ecb6cb4586b3548cfd378e42a6120c97d7496e27fabe3c.
The package includes extraction provenance and the inventoried source notices.
Third-party distribution notices require a complete dependency audit before
publication. Windows ConPTY, crash cleanup, cross-process CLI access and
real agent callbacks remain outside this smoke.
