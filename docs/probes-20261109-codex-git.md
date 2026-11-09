# Probe 2026-11-09: `git commit` in a Codex `workspace-write` sandbox from a linked worktree

Question (#4): a worker runs in a linked git worktree, whose git directory (`git rev-parse --git-dir`) and common directory (`--git-common-dir`) lie outside the worktree. Can it `git commit` inside Codex's `workspace-write` sandbox, with and without those directories in `--add-dir`?

This only records what happened. The router's defaults are unchanged.

## Setup

- codex-cli 0.167.0, model `gpt-6.1-sol` (the local default), `-c model_reasoning_effort=low` to keep it cheap, Linux.
- A throwaway repository under `$HOME`, not `/tmp`: Codex's `workspace-write` already makes `/tmp` and `$TMPDIR` writable (`sandbox: workspace-write [workdir, /tmp, $TMPDIR]`), which would hide the result.

```sh
P=$(mktemp -d "$HOME/wfc-probe-XXXX")        # /home/user/wfc-probe-3YPj
git init -q $P/main && git -C $P/main commit -q --allow-empty -m init
git -C $P/main worktree add -q ../wt -b probe
# in $P/wt:  git-dir     = $P/main/.git/worktrees/wt
#            common dir  = $P/main/.git
```

`codex exec` does not accept `-a` (`error: unexpected argument '-a' found`). The approval policy is a top-level option, so the probe runs `codex -a never exec -s workspace-write ...`. The sandbox is the same as the router's (`codex ... -s workspace-write -a never`).

## Case A: git directories not added

```sh
timeout 120 codex -a never exec -s workspace-write -c model_reasoning_effort=low --ephemeral -C $P/wt \
  "Run exactly this shell command once, then reply with only its exit code and the last line of its output: git commit --allow-empty -m probe-a"
```

```
sandbox: workspace-write [workdir, /tmp, $TMPDIR]
codex
128
fatal: Unable to create '/home/user/wfc-probe-3YPj/main/.git/worktrees/wt/index.lock': Read-only file system
tokens used
13,431
```

`git log` in the worktree still showed only `init`. **The commit fails**: the worktree's git directory is read-only inside the sandbox.

## Case B: both git directories added

```sh
timeout 120 codex -a never exec -s workspace-write -c model_reasoning_effort=low --ephemeral -C $P/wt \
  --add-dir $P/main/.git/worktrees/wt --add-dir $P/main/.git \
  "Run exactly this shell command once, then reply with only its exit code and the last line of its output: git commit --allow-empty -m probe-b" </dev/null
```

```
exec
/bin/bash -lc 'git commit --allow-empty -m probe-b' in /home/user/wfc-probe-3YPj/wt
 succeeded in 0ms:
[probe cbdb7eb] probe-b

codex
0
[probe cbdb7eb] probe-b
tokens used
13,761
```

`git log` in the worktree showed `cbdb7eb probe-b` on top of `init`. **The commit succeeds.**

The temporary repository was removed afterwards (`rm -rf $P`).

## Conclusion

- A Codex worker started the way the router starts it (`-s workspace-write`, worktree as cwd, only state and report directories added) **cannot commit** in a linked worktree **when the worktree's git directories lie outside every writable root** it was given: here the workdir, `/tmp`, `$TMPDIR` and the `--add-dir` directories. `index.lock` in the worktree's git directory is then on a read-only file system. If the git directories are already inside one of those roots (a repository under `/tmp`, say), this result does not apply.
- Adding the worktree's `--git-dir` and `--git-common-dir` with `--add-dir` **lets `git commit` succeed** with no other change. This was measured only for the layout above (main repository and linked worktree as siblings under `$HOME`) with codex-cli 0.167.0 on Linux; other layouts, versions and platforms were not tested.
- `dispatch --git` (#7) applies the second result when asked to; the default is unchanged.
- Not tested: a repository whose `.git` is inside the workdir rather than a linked worktree, and `git push` (that also needs the network; see `dispatch --network`).
