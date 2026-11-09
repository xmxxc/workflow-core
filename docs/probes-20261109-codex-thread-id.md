# Probe 2026-11-09: Codex thread id in shell commands

Question (#9): does a shell command run by Codex see the current thread's id, so `wfc relay` can take the controller's thread from its own environment instead of a UUID typed by hand?

Setup: codex-cli 0.167.0, Linux, `CODEX_HOME` unset (so `~/.codex`). The probe ran in a throwaway git directory under a temporary dir, which was deleted afterwards. Two model calls in total, with no quota errors.

## 1. `codex exec`

```sh
timeout 120 codex -a never exec -s workspace-write --skip-git-repo-check \
  'Run exactly this shell command and reply with its output only: echo "T=$CODEX_THREAD_ID S=$CODEX_SESSION_ID"'
```

Key output:

```
session id: 01a1c045-6cbc-7130-9daa-d087275fb216
exec
/bin/bash -lc 'echo "T=$CODEX_THREAD_ID S=$CODEX_SESSION_ID"' in .../probe.o8I5
 succeeded in 0ms:
T=01a1c045-6cbc-7130-9daa-d087275fb216 S=01a1c045-6cbc-7130-9daa-d087275fb216
```

Rollout: `~/.codex/sessions/2026/11/09/rollout-2026-11-09T06-25-37-01a1c045-6cbc-7130-9daa-d087275fb216.jsonl`. Its first line (`session_meta`) has `id` = `session_id` = `01a1c045-6cbc-7130-9daa-d087275fb216`, `source` `"exec"` and `originator` `codex_exec`.

So **`CODEX_THREAD_ID` and `CODEX_SESSION_ID` are both set, and both equal the rollout's first-line `id`.** The rollout's file name ends with the same id.

## 2. `codex exec resume`

```sh
timeout 120 codex -a never exec -s workspace-write --skip-git-repo-check \
  resume 01a1c045-6cbc-7130-9daa-d087275fb216 'Run the same command again; output only.'
```

Key output: `T=01a1c045-6cbc-7130-9daa-d087275fb216 S=01a1c045-6cbc-7130-9daa-d087275fb216`. Codex wrote no new rollout. It appended to the same file, so there was still exactly one rollout with that id.

So **after a resume, the id is unchanged, and it is still the original rollout's id.** The interactive `codex resume` (TUI) was not run. It is expected to behave the same way, but this was not measured.

## 3. Sub-agents

Not measured. No sub-agent was spawned for this probe. For reference, existing sub-agent rollouts on this machine have `source: {"subagent": {"thread_spawn": {"parent_thread_id": ..., "depth": 1, ...}}}` and `thread_source: "subagent"`, and their own `id`. If a sub-agent runs `wfc relay --codex`, its `CODEX_THREAD_ID` is presumably its own id. That id is caught by the check below because its rollout is a sub-agent's.

## Consequences for `wfc relay`

- `relay --codex` takes the thread from `$CODEX_THREAD_ID`.
- Every Codex thread, whether from `--codex` or `--codex-thread`, is checked before relay starts, and a `--detach` relay is checked in the parent. The check opens only `$CODEX_HOME/sessions/**/rollout-*-<id>.jsonl` (Codex puts the id in the file name). It requires that the first-line `id` equals the thread and that the rollout is not a sub-agent's. When `CODEX_THREAD_ID` is set and differs from the thread, relay is refused. Thread names are refused, because they cannot be checked.
- When `codex queue` answers `no rollout found for thread id …` (seen in example-project #56), the batch definitely did not arrive. Relay puts it back as undelivered, reports the error on stderr and exits 1.
