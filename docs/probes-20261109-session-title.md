# Probe 2026-11-09: setting a host session title at launch

Question (#6): can the router give each worker's host session a title (the dispatch label, `<project> | <ticket> | <role>`) when it launches it, with Claude Code, Codex and OpenCode?

All of this comes from the local `--help`, the installed binaries and OpenCode's own API description. No model was called.

## Claude Code 2.1.319: yes, `-n/--name`

`claude --help`:
```
  -n, --name <name>                     Set a display name for this session
                                        (shown in the prompt box, /resume
                                        picker, and terminal title)
```
The router passes `--name <label>` at dispatch and passes it again on `resume`, so the name stays the same.

## Codex 0.167.0: no launch option; only after launch

- `codex --help` and `codex resume --help` have no name or title option. `codex resume [SESSION_ID]` accepts "Session id (UUID) or session name", and `archive`/`delete`/`unarchive` also take a session name, so sessions do have names. They are kept in `$CODEX_HOME/session_index.jsonl` (`{"id", "thread_name", "updated_at"}`; most entries there were titled automatically).
- No `-c` key sets the name. The `-c` help lists only examples, and the binary has `thread_name` only as a field of the session index and thread metadata, not as a config key.
- The binary (`codex-linux-x64/.../bin/codex`) does have two ways to rename a thread that already exists:
  - the TUI slash command `/rename` ("/rename** to rename your threads for easier thread resuming"), which would have to be typed into the worker's screen;
  - the app-server JSON-RPC method `thread/name/set`, which emits `ThreadNameUpdated`.

  Both need the thread to exist first, so they can only be used after launch, once its rollout and id are there.

## OpenCode 1.19.5: no launch option for the TUI; only after launch

- `opencode --help` (the TUI the router starts: `opencode <dir> --prompt ...`) has no title option.
- `opencode run --help` has `--title  title for the session (uses truncated prompt if no value provided)`, but `run` is the non-interactive command, not the TUI the router uses.
- HTTP API: `opencode serve --port 4799` was started in an empty scratch directory, `/doc` (OpenAPI) was read, and the server was stopped. It lists
  `PATCH /session/{sessionID}`, "Update session: Update properties of an existing session, such as title or other metadata", with body `{"title": string, "metadata": object, "permission": ..., "time": {"archived": number}}`.
  So a title can be set once the session exists, through a server on the same data: the worker TUI's own server (it would need a fixed `--port`) or a short-lived `opencode serve`.

## tmux window name

The router names the worker's tmux window with the label (`new-session -n <label>`), and the session keeps `wfc-<id>`. Checked against real tmux on a private socket (`tmux -L`, killed afterwards):
```
wfc-d-probe [proj | 113 | impl] auto=0
```
The `|` characters are kept, and giving `-n` turns `automatic-rename` off, so the running host does not rename the window, provided the user's tmux does not turn on `allow-rename` (with it on, a program can still rename the window through an escape sequence). Control characters (newline, tab, escape) are turned into spaces before the label is used.

## Decision (controller, 2026-11-09)

The router sets the title only where the host takes it at launch: Claude Code `--name`. Codex and OpenCode titles are not set. Doing it would mean a deferred step after launch (in `wait`/`relay`) that finds the host session and calls a host-internal interface. If that is wanted later:
- Codex: once `find_host_session` finds the rollout, call app-server `thread/name/set` with the thread id and the label;
- OpenCode: once the session is in `opencode.db`, `PATCH /session/{id} {"title": label}` on a server over the same data (the TUI started with a fixed `--port`, or a short-lived `opencode serve`).
