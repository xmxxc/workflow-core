# Bundled execution kernel experiment (#26)

`dispatch --backend bundled` selects the optional co-located `wfc-kernel`
(`wfc-kernel.exe` on Windows). `WFC_BUNDLED_KERNEL` selects an explicit binary.
There is no automatic download or backend fallback. Existing dispatches keep their
recorded backend. This is an opt-in experiment; it does not change the default.

Each execution has its own detached Python runtime and the extracted Rust PTY
kernel. The Rust kernel retains the patched portable-pty, native ConPTY support,
Ghostty terminal parsing, terminal query replies and interactive stdin/stdout.
The Python layer owns a bounded, authenticated loopback JSON transport; independent
`wfc` invocations reconnect to the same runtime. It is not a terminal-output pipe.
A Python 3.9+ installation and the chosen host CLI remain prerequisites.

## Ownership and protocol

The router commits its existing lifecycle fence before starting the runtime.
The backend handle and private launch snapshots record execution attempt, unit,
random nonce and generation, service/kernel/guardian PID and creation identities,
and Linux cgroup inode/device/path. The runtime also checks the Rust kernel's
independent generation and unit nonce. The RPC server proves possession of a
private capability for each fresh client challenge before the client sends input.
The capability is kept in an owner-private snapshot, excluded from public status.
Each frame is bounded to 2 MiB and each socket operation has a deadline. Input is
sent once. A partial write, missing receipt or response identity mismatch remains
uncertain; it never triggers a resend, service restart or silent reattachment.

The router continues owning rounds, callbacks, message receipts, controller fencing
and lifecycle reconciliation. A live execution can serve later task rounds. Resume
creates a new execution identity after confirmed cleanup; it cannot recover a live
PTY from a dead service. A dead parent alone is not proof of a stopped process tree.
The runtime empties the whole container before publishing confirmed absence.

## Linux platform contract

Set `WFC_BUNDLED_CGROUP_ROOT` to an existing writable delegated cgroup v2 subtree
with `cgroup.kill`. A directory that merely contains similarly named files is
refused. The backend never mounts cgroupfs, requests sudo or changes global systemd
configuration. Without this capability, dispatch fails before any worker starts.

The guardian creates a fresh random child cgroup and keeps an open directory handle.
The runtime moves the idle kernel into it before requesting any PTY spawn. Normal
close, kernel death and parent-first worker exit kill and observe the entire cgroup,
including `setsid()` descendants. If the runtime is SIGKILLed, the separate guardian
waits for that exact child and then kills and observes the same cgroup. Cleanup
has a bounded deadline; an unconfirmed result blocks replacement rather than
claiming successful cleanup. Empty cgroup removal can lag cleanup confirmation.

This is a userspace lifecycle guarantee while the guardian survives. Simultaneously
killing the guardian and runtime, host shutdown, an unkillable kernel task, or a
privileged/same-user hostile worker moving itself out of the delegated hierarchy
is outside that guarantee. It is not a security sandbox. A dedicated systemd unit
with its own OS cleanup policy is an appropriate additional test boundary.

## Native Windows platform contract

The runtime creates an unnamed, non-inheritable Job Object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, assigns the idle kernel, then requests PTY
spawn. Workers and descendants inherit the Job; breakaway is not enabled.
Normal close terminates the Job and waits for zero active processes. Forced runtime
death closes its sole handle, letting Windows terminate the contained tree.
Incompatible Job assignment fails before a worker starts. Interactive host stdin
and stdout still belong to ConPTY.

A forcibly killed Windows runtime cannot publish a cleanup receipt. Subsequent WFC
commands therefore retain unknown ownership/liveness until explicit reconciliation,
even when native tests independently observe the descendant tree stopped. No
automatic resume is authorized by a missing process ID alone. Native Job nesting,
parent-first behavior and forced termination must pass the new CI suite; the supplied
raw-stdio ConPTY baseline alone does not prove these lifecycle properties.

## Building and acceptance

Use the pinned extraction/build method in
`experiments/herdr-kernel-prototype/prepare.py`. Rust sources and Cargo lock are
unchanged by this runtime adapter. Preserve all existing upstream source hashes,
portable-pty patches and Ghostty/ConPTY dependencies. The selected upstream source
closure remains 1,378 files / 20,553,673 bytes before build output. Binary size does
not measure source-maintenance cost. The separately maintained dependency notice
collector must be run with the actual target, toolchain and lock before distribution.

```sh
python build.py --out 'trial with spaces/wfc.pyz'
# Co-locate the compiled herdr-kernel-prototype as wfc-kernel (or .exe).
# Native suite selects the real companion explicitly; no model/account is used.
WFC_BUNDLED_TEST_KERNEL=/absolute/path/wfc-kernel \
  python -m unittest discover -s tests -p test_bundled.py -v
```

On Linux run `tools/verify_bundled_linux.sh REPOSITORY KERNEL LOG_DIRECTORY` inside
one disposable delegated systemd user unit. It derives its own cgroup, records the
source identity, executes source and relocated zipapp acceptance, and checks for
leftover child cgroups. The controller must stop that test-owned unit even after a
failure. `.github/workflows/bundled-kernel-experimental.yml` consumes the verified
Windows baseline build method and runs the same native cross-process acceptance.

For an optional portable package, supply the compiled binary and the independently
collected notice/provenance directory together:

```sh
python build.py --windows-dir 'trial with spaces' \
  --bundled-kernel prepared/target/release/herdr-kernel-prototype.exe \
  --kernel-notices collected-notices
```

The build copies the companion under its discovery name, preserves the supplied
notices and writes `BUNDLED-MANIFEST.json` with SHA-256 digests. This packaging hook
records supplied inputs; it does not certify notice completeness or native
acceptance. Controller-owned publishing must retain the experimental label, exact
commit, checksums and manual instructions. It must not replace a formal release.
Authenticated model tasks and desktop/session probes are separate acceptance work.
