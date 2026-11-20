# Bundled kernel EXPERIMENTAL: Windows candidate and manual handoff

This is an opt-in independent CLI experiment for #26. Use `--backend bundled` explicitly. Python 3.9+ and a separately installed, already authorized Codex or OpenCode host are required; host discovery/login is the operator's responsibility. Claude is disabled. Existing backend defaults, tmux, dispatches and stable releases remain unchanged.

## Candidate, CI and publication boundary

The dedicated `bundled-kernel-experiment.yml` builds on Windows 2022 with Rust 1.96.1, Zig 0.16.0 and fixed Herdr source `ee5bfb5041bb3e4a49d591c72308aae3c81ecdde`. It uses this checkout's extraction preparation, authored Rust and Cargo.lock, then the existing `build.py` companion API. No complete external Herdr installation is needed. Git autocrlf is disabled only in CI's process environment. Cargo build and the locked/offline target notice collector share `RUSTUP_TOOLCHAIN` and `CARGO_HOME`.

Only same-repository PR heads under `experiment/bundled-kernel-*` or `codex/bundled-kernel-*`, with relevant path changes, qualify for internal native artifacts. The controller freezes the candidate before starting CI. Independent review may run in parallel with this internal CI; both must identify exactly the same commit/tree. Any correction creates a new candidate and requires relevant validation again. PR artifacts are internal evidence until review and all required verification pass.

The workflow runs the real controlled cross-process suite twice: source first, then the ZIP extracted into a fresh directory with spaces. The relocated suite uses `WFC_TEST_PROGRAM` pointing at the extracted pyz and `WFC_BUNDLED_TEST_DISCOVERY=1`; its fixture removes the explicit binary override before child dispatch, proving co-located companion discovery. The `wfc.cmd` version/help entrypoints are also executed. Required native cases cover dispatch/question/reply/done, received/delivered and processed ACK, close, wrong nonce/generation/attempt, interrupt/resume, parent-first descendant cleanup and forced runtime death/Windows Job cleanup. Windows tree cleanup is distinct from Linux setsid/delegated-cgroup coverage; Linux-only skips must remain visible. Missing or skipped required native cases fail the workflow's coverage gate.

Command logs and actual exit codes, commit/tree, OS/build, Python/Rust/Cargo/Zig versions and package/binary sizes are retained in the native evidence artifact. Tests use the checkout's controlled Python worker with an isolated state and empty Codex home: they need no account, real model, Desktop queue or production daemon. Test cleanup is owned by the fixture and bounded by process identity; the workflow has a 60-minute job ceiling. This does not prove actual Windows host/manual acceptance.

After independent review and all verification pass, the controller may create an **annotated** `kernel-experiment-*` tag at the reviewed commit. Its annotation must contain these lines with actual values:

```text
Reviewed-commit: <full 40-character candidate SHA>
Native-CI-run: <successful exact-SHA PR run ID for this workflow>
```

The tag workflow checks that run, rebuilds/tests/packages the same tree, and publishes an explicitly EXPERIMENTAL prerelease with `--latest=false` and SHA256SUMS. The annotation is the controller's review attestation; CI does not independently perform the human review. No `v*` tag, version change or replacement of the stable v0.4.6 assets occurs. The old `kernel-native-probe.yml` remains a separate historical direction-branch probe; it must not be used as this backend's acceptance or user bundle.

## Provenance and coverage bounds

The ZIP contains `COMMIT.txt`, `EXPERIMENT.json`, `BUNDLED-MANIFEST.json`, WFC's license, upstream/dependency notices and the target Cargo.lock. `bundled-notices/provenance` preserves the original per-file extraction manifest, actual compiled source hashes, portable-pty/Ghostty patch records and patch files. The outer manifest covers notice/provenance files and binary digests; the ZIP checksum covers the manifest itself. Verify the SHA256SUMS obtained with the release before extraction; the manifest is an integrity check, not a signature.

Counts are recomputed from the actual candidate's target metadata, not copied from the historical 52 Windows / 41 Linux Rust packages and 8 materialized Zig inputs. `sources.json` explicitly describes Cargo target metadata including build dependencies and fetched Zig inputs, not a link map. The materialized theme input `N-V-__8AAEFmBABuDGOKxAI6VMg41b9euMZ-z7HS9EcUdaor` has no top-level notice in the inspected package. At fixed upstream `ee5bfb5041bb3e4a49d591c72308aae3c81ecdde`, the [VT build invocation](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/build.rs#L74-L95) uses `-Demit-lib-vt` and `-Demit-xcframework=false`. The [theme resource step](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/libghostty-vt/src/build/GhosttyResources.zig#L128-L138) materializes the package; with the [VT-only default `emit_exe=false`](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/libghostty-vt/src/build/Config.zig#L453-L457), the [resource-install conditions](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/libghostty-vt/build.zig#L159-L259) are inactive and the declared static-VT/kernel graph excludes theme data.

Inspection of the historical Windows ZIP for commit `b802a4c0dda83e4956f04c949d37b20fb76603e4` (ZIP SHA256 `43337582eec25b74621fbba89a0c6df9c043db0d51eb1e545800010d5afb23d5`) verified theme references in metadata/documentation and no theme source/data payload. This resolves the archive-payload and pinned-source-graph questions for that build. Rebind the finding to each final bundle by verifying its commit/tree, native source hashes, ZIP checksum and complete file manifest. No empirical Windows linker map was obtained; this theme-specific finding does not establish universal binary or legal approval or an audit of other dependency licenses.

## Windows PowerShell manual acceptance

Start only with a reviewed candidate and passing exact-SHA native CI. This is a foreground `wait`/reply handoff; #35's stopped Desktop probe and its A/D matrix remain separate. Historical #30 rc.4 manual success does not establish bundled-backend success. Do not rerun either matrix as part of this guide.

Set paths to the unpacked candidate and an actual installed host. The user-verified Windows Codex 0.164.0 invocation uses `--no-daemon --model gpt-6.1-sol` and process `TERM=xterm-256color`. For Codex JS, use the actual JS file plus explicit Node runtime; for a native Codex host use `command = @('C:\actual\codex.exe', '--no-daemon', '--model', 'gpt-6.1-sol')` and omit `runtime`. Do not use an npm `.cmd` shim. Keep the existing authorized host login configuration; do not copy credentials, change global PATH, or automate login. These invocation settings do not establish bundled-backend manual acceptance, which remains pending; Claude remains disabled.

```powershell
$ErrorActionPreference = 'Stop'
$wfc = 'C:\Tools\bundled-experiment\wfc\wfc.cmd'
$node = 'D:\nodejs\node.exe'
$hostJs = 'D:\nodejs\node_modules\@openai\codex\bin\codex.js'
foreach ($p in @($wfc, $node, $hostJs)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "Set actual installed path: $p" }
}
$bundle = Split-Path $wfc -Parent
Get-Content "$bundle\COMMIT.txt", "$bundle\EXPERIMENT.json"
& $wfc --version
python --version
& $node $hostJs --version
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber
$work = Join-Path $env:TEMP ('wfc-bundled-manual-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory "$work\reports" | Out-Null
Remove-Item Env:WFC_DISPATCH, Env:WFC_OWNER_TOKEN, Env:WFC_CONTROLLER_TOKEN, Env:WFC_BUNDLED_KERNEL, Env:WFC_STATE, Env:WFC_LAUNCH_CONFIG, Env:TERM -ErrorAction SilentlyContinue
$env:WFC_STATE = "$work\state"
$env:TERM = 'xterm-256color'
$launch = @{ hosts = @{ codex = @{ command = @($hostJs, '--no-daemon', '--model', 'gpt-6.1-sol'); runtime = @($node) } } } | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText("$work\launch.json", $launch, [Text.UTF8Encoding]::new($false))
$env:WFC_LAUNCH_CONFIG = "$work\launch.json"
@'
Use only this scratch directory. Invoke the exact WFC ask command in your brief once with BUNDLED_READY?, then end the turn. On BUNDLED_GO, write BUNDLED_PASS to the assigned report, invoke the brief's exact done command, and end the turn. Preserve its dispatch, attempt and message ID. Do not ask twice; on resume inspect prior work first.
'@ | Set-Content -Encoding ascii "$work\task.md"
$started = & $wfc dispatch --backend bundled --host codex --cwd $work --task "$work\task.md" --report "$work\reports\result.md" --launch-config "$work\launch.json" | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'dispatch failed; preserve first failure' }
$d = $started.dispatch
& $wfc status --dispatch $d
$questions = & $wfc wait --scope $started.scope --timeout 90 | ConvertFrom-Json
$questions.messages | ConvertTo-Json -Depth 12
# Set these from the actual BUNDLED_READY? envelope, preserving all identities.
$question = $questions.messages | Where-Object { $_.dispatch -eq $d -and $_.question -eq 'BUNDLED_READY?' } | Select-Object -First 1
if (-not $question) { throw 'No matching question; preserve diagnostics' }
& $wfc reply $question.id 'BUNDLED_GO'
& $wfc ack $question.messageId
$messages = & $wfc wait --scope $started.scope --timeout 90 | ConvertFrom-Json
$messages.messages | ConvertTo-Json -Depth 12
$done = $messages.messages | Where-Object { $_.dispatch -eq $d -and $_.type -eq 'done' } | Select-Object -First 1
if (-not $done -or $done.attempt -ne $started.attempt) { throw 'Missing current-attempt done' }
Get-Content "$work\reports\result.md" # inspect the actual report before ACK
& $wfc ack $done.messageId
& $wfc status --dispatch $d # received/delivered -> processed; completed
& $wfc close $d
& $wfc status --dispatch $d # verify backendAlive false and owned children gone
```

Record actual exit codes and full sanitized command results. Success requires the host's canonical conversation to contain exactly one complete router answer for the actual question, no manual Enter/extra keys, the correct report and same dispatch/attempt, done received/delivered then ACK processed, and close with confirmed owned process-tree exit. Screen text or exit 0 alone is insufficient. Capture service/kernel/guardian/descendant PIDs and creation identities before close; verify those identities exited afterward. If any step fails or is uncertain, preserve it and close only this dispatch; never kill a PID without matching its recorded creation identity.

For every Windows run, create a handoff record with commit/tree, ZIP and source/binary hashes, OS/toolchains/host versions, launch argv (redacted), scope/dispatch/round/attempt/message IDs, canonical host-session evidence, lifecycle/cleanup results and remaining gaps. Add it to #10's total index whether **success, failure or blocked**, retaining the first failure scene and linking follow-up attempts instead of replacing it. Record controlled native CI and real-host manual results separately. A blocked login/configuration or unknown cleanup is not a manual pass.
