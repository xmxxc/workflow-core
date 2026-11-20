# Windows foreground creation evidence (#38)

This is a diagnostic plan and revised frozen controlled probe, based on WFC commit
`8861e833b13cc31b2641be597d40012d21390299` / tree
`89238098eb2f8f1997916792b96123db93ca6f2b`. Window attribution and interactive
desktop regression remain **pending**. Production creation flags are unchanged.
The controller arranges independent candidate review and any new native run.

## Retained observation

The supplied first timeline is dated **2026-11-19 UTC+08:00**. It concerns
dispatch `d-538aec2e`, attempt `a-f65a3e26c907`, candidate `8861e833`.

| Local time | Retained observation | What remains unknown |
| --- | --- | --- |
| 13:28:59.188044 | dispatch starts, PID46604 | entry creation identity and intervening wrapper identities |
| 13:28:59.5704177 | guardian33232, parent PID51292 | parent's creation identity and relationship to entry46604 |
| 13:28:59.8374140 | runtime52144, guardian parent | actual creation flags and console attachment |
| 13:28:59.8436650 | conhost26412, runtime parent | console/pseudoconsole/Terminal session mapping |
| 13:28:59.971524 | SHOW HWND0x00380DBC, CASCADIA_HOSTING_WINDOW_CLASS | which operation requested display |
| 13:29:00.032506 | dispatch interrupted | exact cancellation boundary |
| 13:29:00.2599813 | kernel50508 | full creation/parent identity records |
| 13:29:00.5614515 | codex27884 | full creation/parent identity records |

The SHOW owner is an existing WindowsTerminal.exe PID24148, created November15
21:17:00 +08:00. Its parent PID29876 lacks a parent creation identity in this
summary. Runtime conhost creation precedes SHOW by **127.859ms**. Kernel and Codex
creation follow SHOW in the supplied timeline. These facts prioritize the
guardian→runtime boundary; they do not identify the triggering call or establish
that WFC created this existing Terminal process. No date is inferred from the
worker's Linux clock. The complete original eleven identities and raw window
records are still unavailable here. Their summarized exit status and signed
cleanup evidence concern lifecycle cleanup, independently of window attribution.

The preserved source documents are controller-managed
`windows-foreground-first-timeline.md` and
`windows-foreground-timeline-direction.md`; no original was rewritten.

## Creation boundaries at the fixed source

Source links below are repository-relative. Values describe code, not measured
Win32 creation telemetry from the failed desktop run.

| Boundary | Code / launch structure | Existing observation / gap |
| --- | --- | --- |
| User terminal/shell → cmd entry → Python CLI | [build.py](../../build.py), build_windows: wfc.cmd invokes python + wfc.pyz; [wfc.py](../../wfc.py), router cmd_dispatch | caller-dependent entry/wrapper processes; no shell/entry creation chain recorded |
| CLI → guardian | [bundled_backend.py](../../bundled_backend.py), create: Python WFC `_bundled-guard` + private spec; DEVNULL stdio; [wfc_platform.py](../../wfc_platform.py), detached_options | requested Windows DETACHED_PROCESS0x8 + CREATE_NEW_PROCESS_GROUP0x200, close_fds; guardian PID/creation saved; no HWND or console-session mapping |
| guardian → runtime | [bundled_runtime.py](../../bundled_runtime.py), guard: Python WFC `_bundled-runtime`; DEVNULL stdio, close_fds, no explicit creationflags | trace runtime_started has PID/creation, elapsed time; no effective native flags or console ownership; primary controlled comparison |
| runtime → kernel | bundled_runtime.Kernel: companion executable, stdin/stdout pipes, stderr DEVNULL, close_fds, no explicit creationflags | kernel PID/creation, hello and later runtime snapshot; kernel enters Job before spawn; no window event capture |
| kernel → ConPTY infrastructure | [src/main.rs](../../experiments/herdr-kernel-prototype/src/main.rs), spawn: native_pty_system().openpty | OS may involve conhost/brokering; infrastructure identity/session relation is not returned by WFC; presence does not establish display causation |
| kernel/ConPTY → host supervisor | src/main.rs pair.slave.spawn_command(CommandBuilder); argv is Python WFC `_host-run` | worker_pid only in runtime snapshot, without worker creation identity; needs exact native parent/creation evidence |
| host supervisor → configured runtime/script/host | [host_launch.py](../../host_launch.py), configure/prepare/supervise; native runtime prefix optional; Popen with terminal stdin/stdout and stderr pipe, no explicit flags | started receipt has child PID/processStart and wall time; no parent creation, flags or window correlation; receipt later overwritten on exit |
| host → helpers / callback Python / shell | host behavior, [wfc_platform.py](../../wfc_platform.py), callback_command: PowerShell Start-Process -NoNewWindow -Wait when used | dynamic helpers and broker activation are not exhaustively traced; -NoNewWindow alone is not ownership or absence proof |

Windows `.cmd`/`.bat` hosts are refused by host_launch.platform_argv unless an
explicit native runtime/script arrangement is configured. The outer packaged
wfc.cmd is a separate entry boundary. Do not merge these two launch paths.

The prepared portable-pty source is not vendored in this checkout. Read-only
inspection verified two files exactly against upstream pin
`ee5bfb5041bb3e4a49d591c72308aae3c81ecdde`, selected by prepare.py:

- vendor/portable-pty/src/win/conpty.rs: spawn_command delegates to PsuedoCon;
  SHA256 `8af0d7654aad62aaa67685b7b8550bc638b732360f7de1357cc5b0a35bc47ca1`.
- vendor/portable-pty/src/win/psuedocon.rs: CreatePseudoConsole uses its cursor,
  resize and input-mode flags; CreateProcessW uses STARTUPINFOEXW with a PTY
  attribute, STARTF_USESTDHANDLES and invalid standard handles, then
  EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT (0x80400);
  SHA256 `f5c46dcf558a482544a9d783fe8c1bb26f409b6086f8def90b553aebe01fabf3`.

This source inspection does not prove the failed artifact executed those exact
native calls. Preserve artifact provenance and native measurements separately.

## Minimum evidence ledger

All currently available field material has been supplied. Preserve its unknowns;
preparing/running the isolated new-candidate comparison does not depend on obtaining
unavailable historical originals. Do not request the same batch or an old-package
rerun. The correlation field map, limited to the relevant interval, is:

| Record | Allowed fields / matching rule | Current gap |
| --- | --- | --- |
| Time basis | date, UTC offset/time zone, wall-clock and monotonic anchors, precision/error bounds; WinEvent low32 tick rollover mapping | date/offset now known; event source/clock conversion and error bounds absent |
| Each relevant process | PID + exact creation FILETIME; executable basename; parent PID + parent's creation FILETIME; creation/exit times, observation source | entry46604, parent51292 and Terminal parent29876 incomplete; full eleven-identity original not available |
| Each create edge | creator identity, child identity, call/return times, API/layer, numeric flags, structured stage/argv shape; redact every argument value | failed run flags absent; no inference from current defaults |
| Console / PTY | sampled identity, console HWND, attached PID+creation set; conhost identity; pseudoconsole/session relation where actually observed | no authoritative conhost26412 ↔ Terminal HWND/session/activation mapping |
| SHOW | event occurrence timestamp plus callback arrival time, HWND, class, owner PID+creation, object/child id, source/loss status | owner summary present, complete raw event/identity/time mapping absent |
| Scope / cleanup | candidate hashes, controlled identities, capture start/end, lost events, root reap + Job-empty outcome | historical cleanup summarized; new comparison not run |

Keep original records with their existing custodian; any diagnostic projection
uses only the minimum metadata and its integrity hash. Do not dump environment, command lines, private
launch specs/snapshots, capability/token/nonce, user paths, window titles,
terminal buffers or model prompts. Basenames, structural argument roles and
numeric flags suffice. Preserve unknowns; never fabricate a creation identity
from PID, rounded text, class name, temporal proximity or a role label.

Existing LifecycleTrace is bounded/opt-in with per-role monotonic elapsed times
and a narrow field allowlist. It has no common wall-clock anchor, HWND events,
complete parent edges or console-session mapping. The private runtime snapshot
contains capability material and paths and must not be copied wholesale into
diagnostic evidence. Existing signed cleanup receipts are not window provenance.

## Frozen native comparison tool

[probe_windows_foreground.py](../../tools/probe_windows_foreground.py) is an
isolated diagnostic harness, **not yet executed on native Windows**. It imports
the existing WindowsContainment and native_probe from this same source tree;
it does not edit them. It never issues a WFC dispatch, opens an account/model,
builds/downloads anything or closes a pre-existing Terminal.

For a controller-arranged run after independent new-candidate review, use an
explicit local kernel from that reviewed candidate and its expected SHA256:

```powershell
python tools/probe_windows_foreground.py --kernel $ReviewedKernel --kernel-sha256 $ReviewedKernelSha256
```

The two trials use detached guardian creation as in the fixed source. Only
guardian→runtime changes: requested flags0 versus CREATE_NO_WINDOW0x08000000.
Both variants use identical redirected stdio and the existing real kernel
ConPTY control: READY → generated input → reversed response → close/reap.
Only pass/fail and allowed process/console metadata are retained; generated
input/terminal text and protocol nonce are not output.

Each guardian waits on an input gate until assigned to its unique kill-on-close
Job. The parent bounds each trial at35s; Job stop has the existing6s bound and
root reap5s plus one exact-handle emergency5s reap. Cleanup attempts Job-empty,
checked handle closure and root reaping even after a failed gate/child. Fresh
temporary configuration/evidence roots are removed on completion; deletion,
cleanup or timeout errors make the run fail rather than silently pass. Parent
death closes its sole Job handle. No cleanup targets are inferred from external
PID lists or watched windows. No test child can intentionally break away.

The fail-capable gate control is prepared for the same arranged native session:

```powershell
python tools/probe_windows_foreground.py --kernel $ReviewedKernel --kernel-sha256 $ReviewedKernelSha256 --control hold-guardian-gate
```

Expected: controlled comparison FAIL/exit1 after a2s unreleased-gate deadline,
with root reaped and Job-empty evidence. This negative native control is
**pending**, as is normal native I/O and forced-timeout cleanup verification.

Window observation is always configured. A global EVENT_OBJECT_SHOW hook is
necessary to see new HWNDs/owners and brokered Terminal candidates outside the
owned Job. It is limited to40s,512 window-object callbacks for metadata queries,
and64 retained non-control events. The pump handles at most256 messages per
iteration and disables collection on budget exhaustion. Loss/budget exhaustion
is explicit and makes the observation incomplete.

Each trial creates two1x1 empty-title STATIC popup windows in the probe's own UI
thread, one before work and one afterward, and calls SW_SHOWNOACTIVATE. Actual
SHOW delivery must be observed by the same installed hook. Each control's HWND,
owner PID+creation, thread, event clock, and exact DestroyWindow outcome are
recorded. Only those handles created by the probe are shown/destroyed. Destruction
checks owner creation and thread, retries that same exact handle once on failure,
retains the original error, and never targets an existing Terminal. Parent exit
also ends its own windows. A missing positive event, owner-query/clock failure,
failed cleanup or failed unhook prevents usable observation. These native paths
are prepared, not native-tested.

After transient classification the collector discards unrelated class/identity
records. It retains only test-owned Job windows (any class), exact optional
watched-owner windows, positive controls, and these narrowly selected broker
candidates: CASCADIA_HOSTING_WINDOW_CLASS, ConsoleWindowClass, PseudoConsoleWindow.
A candidate can belong to an unrelated Terminal; its relation is explicitly
`unattributed_terminal_candidate`, not a WFC attribution. This small class set
allows a new broker HWND/owner to be noticed without reading titles, executable
paths, command lines, environment or content. Other unrelated metadata is not
retained. The collector is not a general desktop monitor.

Optional read-only seed arguments are `--watch-pid`, `--watch-creation
windows:<exact FILETIME>`, and `--watch-hwnd 0x<exact HWND>`, all together. The
seed identity is verified; subsequent SHOW events for new HWNDs with that exact
owner are also eligible. An absent seed does not disable global/owned collection
or the positive controls. Never manipulate a pre-existing Terminal for a control.

Owner creation/exit FILETIMEs and sampled liveness come from one process handle,
with Job membership queried on that same handle. Window PID is rechecked around
sampling; identity remains explicitly `sampled_after_event`. Raw WinEvent low32
millisecond ticks, UTC/GetTickCount64 brackets, a mapped UTC interval padded conservatively by two queried system time
increments with a32ms floor (not a1ms-resolution claim), callback arrival,
clock/lifetime ambiguity, loss and capture
bounds are retained. The timer increment and chosen padding are recorded;
unavailable resolution leaves UTC endpoints unknown, preserves the raw event
and makes observation incomplete. Endpoint UTC-versus-uptime drift is recorded
and drift beyond the padding also makes it incomplete. Native calibration of
this estimate remains a gate. They cannot prove the owner at a past event from a later
sample, nor prove the parent/broker activation chain. Reuse/query/clock uncertainty
therefore remains explicit; a contradictory lifetime makes observation incomplete.

Positive HWND/owner/thread and initial cleanup state are registered immediately
after creation, before fallible clock sampling. Failed post-create clock sampling
therefore cannot hide an uncleaned owned window or permit another tree launch.

The JSON separates `io_flags_result` from `window_observation_result`. The latter
is `usable` only with an installed/closed hook, both observed/destroyed positive
controls, and no known loss, ambiguity, budget or observation error. Otherwise
it is `incomplete`, even if ConPTY/flags passed. Exit0 requires both results and
cleanup to pass their limited contracts; **native_attribution stays pending**.
Usable means a healthy bounded sample in the defined scope, not provider-wide
completeness or no-popup proof. Zero candidate events never proves absence;
unknown classes/new activation paths and OS event-delivery uncertainty remain.

Each trial is appended before execution. Its already read flags, identities,
console and Job/root cleanup records survive first/later trial failure or a
TemporaryDirectory cleanup exception. Errors are appended to the affected trial
without echoing exception text or replacing earlier evidence. Cleanup errors make
the run non-PASS. Failed evidence reads and unavailable clock samples are explicit.
After uncertain hook/owned-process/control-window cleanup, later trials are
recorded as skipped instead of starting another owned tree. Temporary files are
removed when possible; on deletion failure the sanitized in-memory report survives,
with failure visible. It does not silently discard the original failure.

Successful Popen return immediately records `root_created=true`, before Job
attachment or process-identity sampling. Every created root must have confirmed
`root_reaped=true` before a later launch, independently of guardian identity
metadata and cleanup error categories. An empty Job does not prove an unattached
root exited. A failed creation leaves root_created false; a created and confirmed
reaped root can permit a later trial even if attachment failed. Exact Popen-handle
cleanup and every collected record/error remain unchanged.

The tool records actual **CPython _winapi.CreateProcess argument flags** for its
entry→guardian, guardian→runtime and runtime→kernel edges. This is distinct from
the final Win32 CreateProcessW flags after CPython/OS processing; an API trace
is still needed where that distinction matters. No speculative production flag
change follows from this comparison. GetConsoleWindow/GetConsoleProcessList are
attachment snapshots, not an authoritative conhost↔Terminal activation map;
null/failed/truncated observations remain explicit. Job-empty proves controlled
process cleanup, not absence of desktop effects.

## Offline ledger checker and executed controls

[check_foreground_evidence.py](../../tools/check_foreground_evidence.py) reads one
manually projected JSON file, at most512KiB, up to128 process records and256 SHOW
records over at most five minutes. It launches no process and touches no OS
window/process API. Input is never copied or echoed; only fixed verdict/reason
codes are printed. It is a consistency checker, not a sanitizer or authenticator.

The version1 schema has exactly these fields (no arbitrary text/argv/env):

- Root: version, capture_start, capture_end, clock_uncertainty_ms, lost_events,
  entry, processes, shows. Dates are ISO8601 with explicit offset or Z and up to
  seven fractional digits. entry is `{pid, creation}` with exact
  `windows:<FILETIME integer>` identity.
- Process: pid, creation, parent (identity or null), basename, stage,
  creation_flags (unsigned integer or null), exited_at (offset timestamp or
  null meaning no exit recorded). Stage is entry/guardian/runtime/kernel/
  host-supervisor/host-runtime/host/helper/terminal/external.
- SHOW: at, hwnd (nonzero hex), class, owner (identity). `at` must be the mapped
  event occurrence time, not callback arrival time. Clock uncertainty bounds
  propagate to event/lifetime checks. Hardware/tick conversion belongs in the
  reviewed projection, with originals retained separately.

Exit0 `review_ready` means only that the supplied owner chain reaches the named
entry without the checked contradictions. Exit1 `pending` includes reused PID
with a missing exact identity, incomplete parent chain, lost events, missing
flags, cyclic/contradictory lifetimes, missing SHOW or ambiguous timing. Exit2
is malformed/unavailable input. **Every verdict keeps native_attribution=pending.**
This cannot infer brokered activation, authenticate a self-reported parent,
prove trace completeness, prove cleanup or establish a negative desktop result.
If the originals lack the required entry creation identity, a valid ledger
cannot yet be projected; report that gap, rather than inventing a value.

The focused Linux command is:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_foreground_evidence.py -v
```

The original15 controls exercise the public checker CLI, including exact identity/PID reuse,
parent chain/lifetimes, missing time zone,100ns timestamp precision, event loss,
unknown flags, clock uncertainty, empty/out-of-scope SHOW, invalid/oversized/
duplicate-key input and non-echo of forbidden fields. The native comparison
CLI refuses Linux before opening a kernel or launching children. Additional controlled models exercise the actual probe main/trial reporting flow
with modeled OS/process boundaries: first/second temporary-cleanup failures,
first/later launch failure, normal reporting and incomplete observation despite
successful I/O. Observer policy is also driven by a modeled native window API:
positive delivery/silence, new HWND/owner capture, unrelated metadata exclusion,
query loss, lifetime/clock contradiction and bounded collection/cleanup errors.
No native process, Job, HWND or hook is created by these models. These are
evidence-contract controls, **not a reproduction or repair of the Windows bug**.

## Next decisions from evidence

All currently available historical material is already supplied. Keep its unknowns
without blocking the isolated contrast or requesting duplicate collection. After
the original independent reviewer checks this revised candidate, the controller
can arrange the documented bounded native negative/normal comparison using an
explicit local reviewed kernel hash. The worker does not self-release it.

Native gates remain: actual owned-window SHOW delivery/cleanup and hook health;
new HWND/owner coverage and loss/clock behavior; matched flags/console/ConPTY I/O
for both variants; failed-gate/timeout/parent-exit Job/root cleanup; and console/
broker attribution where this metadata cannot answer the causal question. Keep
flags, console attachment, SHOW and ConPTY I/O as separate observations. Timing
and a changed SHOW count alone cannot prove the triggering production call.
Subsequent real-host foreground regression uses the new reviewed candidate and
controller arrangements. Historical controlled-host passes stay historical;
hosted CI and Linux models do not prove the interactive desktop is fixed. #35
stays closed.
