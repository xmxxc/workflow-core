# Host launch configuration and diagnostics

WFC starts the selected host with argv and the caller's environment. It does not
run a login shell, source NVM, install runtimes, or change the user's global PATH.
This applies to Codex, Claude Code and OpenCode on the tmux backend.

## Configuration

Pass `dispatch --launch-config /absolute/path/launch.json`, or set
`WFC_LAUNCH_CONFIG` to that JSON file. The CLI option takes precedence. WFC records
the absolute config file path with the dispatch; `resume` and `handoff` reread that
file for the selected host, using the environment of the controller calling them.
Keep the file available and edit it to repair an installation before resuming.
An existing dispatch without a recorded file continues to inherit the caller's
environment; a newly set `WFC_LAUNCH_CONFIG` does not silently reconfigure it.

```json
{
  "env": {"LANG": "en_US.UTF-8"},
  "hosts": {
    "codex": {
      "command": ["/opt/Agent Tools/codex/bin/codex.js"],
      "runtime": ["/opt/Node Runtime/bin/node"],
      "env": {"PATH": "/opt/Node Runtime/bin:/usr/local/bin:/usr/bin:/bin"}
    },
    "claude": {"command": ["/opt/Agent Tools/claude"]},
    "opencode": {"command": ["/opt/Agent Tools/opencode"]}
  }
}
```

`command` is a nonempty argv array replacing the launcher's default executable;
fixed host arguments can follow its first entry. `runtime`, when present, is a
nonempty argv prefix, for example `["/opt/node", "--some-runtime-option"]`.
The resulting argv is `runtime + command + WFC-generated host arguments`.
With no command override, WFC uses `codex`, `claude`, or `opencode`.
All values are literal strings: spaces and Unicode require no extra quoting.
There is no shell expansion of `$VARIABLE`, `~`, globs, pipes, or redirects.
Unknown keys, invalid argv arrays and non-string environment values are rejected.

Environment precedence, from lowest to highest:

1. Environment inherited by the WFC process.
2. Top-level config `env`.
3. Selected host's `env`.
4. Launcher-owned settings (such as OpenCode's permission configuration), followed
   by router-owned `WFC_DISPATCH`, `WFC_STATE`, and the current scope owner token.

Bare executables and Unix `#!/usr/bin/env node` runtimes are looked up using this
final PATH. Relative executable paths and relative PATH entries are relative to
the worker cwd. Absolute executable paths preserve symlinks, including virtual
environment Python entrypoints. An explicit runtime can execute a script without
its executable bit. Direct Unix shebangs and `env -S` are supported; complicated
`env` options require explicit runtime argv instead. No runtime is guessed from
host names. An executable may depend on additional libraries or subprocesses;
errors beyond the resolved entrypoint and its direct interpreter are recorded
from execution rather than guessed from an exit code.

## NVM and a stale terminal server

If Codex's entrypoint uses `#!/usr/bin/env node` and a desktop/background caller
has no NVM bin on PATH, an absolute Codex path alone is insufficient. Configure
its `env.PATH` to include the desired Node bin, or specify both its JS entrypoint
and an absolute `runtime` argv as above. Paths are installation-specific; WFC
contains no username or NVM-version defaults.

The tmux server's inherited environment does not decide host lookup. WFC passes
an exact environment snapshot through a mode-0600 one-use handoff under
`$WFC_STATE/launches` (created mode 0700). The supervisor consumes and deletes it
before launching the child. This prevents a long-running tmux server with a
stale PATH from changing the result. The snapshot contains the inherited
environment and argv; keep the state directory private. If launch acknowledgement
is unknown, an unconsumed `.spec` can remain. After checking that no launcher can
still consume it and explicitly reconciling the attempt, the operator can remove
that specific stale file. Do not remove handoffs while launch is unresolved.

## Results and diagnosis

`wfc status --dispatch d-…` includes `launch`, `signals.fault`, the current attempt,
and lifecycle `audit`. The dispatch command prints the dispatch identity on a
launch failure, including failures before terminal creation, so status can read
its durable diagnostic. Successful `dispatch` means process creation was observed;
it does not prove the host has authenticated or become ready to accept a task.
A short startup observation checks for early exits. Later exits update the same
receipt and remain visible to subsequent status calls.

| Category | Evidence |
| --- | --- |
| `executable_missing` | Configured host entrypoint cannot be found. |
| `runtime_missing` | Explicit runtime or direct Unix shebang interpreter cannot be found in the effective environment. |
| `permission_denied` | OS permission check or process creation denies execution. |
| `process_exited` | Started host exited; `exitCode` and bounded stderr excerpt are retained. |
| `unknown` | No process acknowledgement, unsupported entrypoint, or another launch error. |

Diagnostics keep at most 4096 characters. The supervisor drains stderr with a
bounded memory buffer; it stores no raw stderr log and never dumps the environment
or complete argv into status. Known credential environment values and common
password/token/API-key, bearer and URL credential forms are redacted before
truncation. Normal host stderr still reaches the terminal; treat terminal
scrollback as host output. Diagnostic redaction cannot identify arbitrary secrets
without a credential name or recognizable format. Keep credentials in appropriately
named environment variables, rather than task text or command arguments.

An uncertain startup outcome with category `unknown` retains the attempt and
records an unresolved `create` audit. A preflight refusal with category `unknown`
confirms that nothing was executed; it keeps a diagnostic without an unresolved
audit. The uncertain outcome requires operator inspection and `reconcile` before resume or
handoff. It never automatically redispatches. Existing controller-owner,
attempt fencing, and lifecycle reconciliation rules still apply. A confirmed
missing executable/runtime can be fixed in the config and submitted as a new
explicit dispatch; the failed record remains inspectable.

## Native Windows adapter boundary

The resolver has a native-Windows path: `.exe` entrypoints are argv and are never
interpreted using Unix shebangs. `.cmd` and `.bat` are explicitly refused as
`unknown`, because those entrypoints require shell grammar. Configure the package's
native `.exe` or its actual JS/Python script plus native runtime argv instead.
This boundary is tested on Unix; it does not constitute native Windows backend
support or a real Windows acceptance run. The Windows core and Herdr backend are
separate work items. WFC does not assemble a shell command from host or task data.

## Validation and real Codex probe

The focused suite uses real cross-process controlled hosts and a terminal adapter
with a deliberately stale PATH:

```sh
python3 -m unittest discover -s tests -p test_host_launch.py -v
python3 -m unittest discover -s tests -v
python3 build.py --out /tmp/wfc-host-launch-17
/tmp/wfc-host-launch-17 --version
/tmp/wfc-host-launch-17 --help
```

For a real Codex tmux probe, use a dedicated state directory and a short task
whose only action is to write its assigned report and call the supplied `done`
command. The controller runs this against its fixed candidate, after controlled
validation. It must supply its actual installed Codex JS path and Node path in a
launch config; the paths in this document are examples.

```sh
WFC_STATE=/tmp/wfc-issue17-codex-probe /usr/bin/python3 /tmp/wfc-host-launch-17 dispatch \
  --host codex --launch-config /tmp/wfc-issue17-probe-launch.json \
  --cwd /tmp/wfc-issue17-probe-work \
  --task /tmp/wfc-issue17-probe-task.md --report /tmp/wfc-issue17-probe-report/r1.md
# Use the dispatch id printed above:
WFC_STATE=/tmp/wfc-issue17-codex-probe /usr/bin/python3 /tmp/wfc-host-launch-17 status --dispatch d-…
WFC_STATE=/tmp/wfc-issue17-codex-probe /usr/bin/python3 /tmp/wfc-host-launch-17 wait --timeout 90
WFC_STATE=/tmp/wfc-issue17-codex-probe /usr/bin/python3 /tmp/wfc-host-launch-17 reclaim --dispatch d-… --yes
```

Create the cwd, task and report directory before dispatch. Inspect the completed
receipt and report before reclaiming; stop any probe process before reporting.
Do not run this recursively from an implementation worker.
