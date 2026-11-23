# Install WFC v0.4.7

Download the asset for your platform and `SHA256SUMS` from the [v0.4.7 release](https://github.com/xmxxc/workflow-core/releases/tag/v0.4.7). Each command needs Python 3.9 or later with SQLite. Install and sign in to the host CLI separately.

| Use | Asset | Entry point |
| --- | --- | --- |
| Linux x64 with the bundled kernel | `wfc-v0.4.7-linux-x64.tar.gz` | `./wfc/wfc` |
| Native Windows x64 with the bundled kernel | `wfc-v0.4.7-windows-x64.zip` | `.\wfc\wfc.cmd` (uses `python` on PATH) |
| Existing tmux installation on Linux or macOS | `wfc` and `wfc.sha256` | `wfc` on PATH |
| Standalone Python application | `wfc.pyz` | `python wfc.pyz` |

## Linux x64

The bundled Linux kernel requires glibc 2.39 or later. The standalone Python command can also run on systems where the bundled kernel is unavailable.

From a new directory containing the archive and checksum list, verify before extracting.

```sh
grep '  wfc-v0.4.7-linux-x64.tar.gz$' SHA256SUMS | sha256sum -c - &&
  tar -xzf wfc-v0.4.7-linux-x64.tar.gz &&
  ./wfc/wfc --version
```

Keep the unpacked directory together. For `--backend bundled`, set `WFC_BUNDLED_CGROUP_ROOT` to a writable delegated cgroup v2 subtree with `cgroup.kill` in the controller's environment. In a systemd service with `Delegate=yes` the service can use its own cgroup:

```sh
export WFC_BUNDLED_CGROUP_ROOT="/sys/fs/cgroup$(awk -F: '$1 == 0 {print $3}' /proc/self/cgroup)"
```

## Windows x64

From a new directory containing the ZIP and checksum list, run in PowerShell:

```powershell
$name = 'wfc-v0.4.7-windows-x64.zip'
$entry = @(Get-Content SHA256SUMS | Where-Object { ($_ -split '  ', 2)[1] -ceq $name })
if ($entry.Count -ne 1 -or (Get-FileHash -Algorithm SHA256 $name).Hash -ne ($entry[0] -split '  ', 2)[0]) { throw 'ZIP checksum mismatch' }
Expand-Archive -LiteralPath $name -DestinationPath .
.\wfc\wfc.cmd --version
```

Keep the entire `wfc` directory, including `wfc-kernel.exe` and `bundled-notices`. Use an installed native Codex executable or configure a JS entry point with its Node runtime. Windows 11 acceptance used native Codex CLI 0.164.0 with `--no-daemon`, `TERM=xterm-256color` and an existing login. See the [host configuration](https://github.com/xmxxc/workflow-core/blob/v0.4.7/docs/host-launch.md) and [Windows acceptance record](https://github.com/xmxxc/workflow-core/issues/44).

## Select the backend

The default backend uses tmux on Linux or macOS. Select the bundled kernel on Linux or Windows with `dispatch --backend bundled --host codex` or `--host opencode`. Claude Code uses tmux. The optional `--backend herdr` uses a separately installed Herdr server. Existing dispatches keep their recorded backend.

WFC stores runtime state in the platform's user state directory by default. If you set `WFC_STATE` to a path inside a checkout, use a `.wfc-state` directory to keep its contents out of Git.

For the single-file Unix installation, run `sha256sum -c wfc.sha256` (macOS: `shasum -a 256 -c wfc.sha256`), then `mkdir -p ~/.local/bin && install -m 755 wfc ~/.local/bin/wfc`. For `wfc.pyz`, use the matching row of `SHA256SUMS` and then run `python wfc.pyz --version`:

```sh
grep '  wfc\.pyz$' SHA256SUMS | shasum -a 256 -c -
```

On Windows, compare `(Get-FileHash -Algorithm SHA256 wfc.pyz).Hash` with the `wfc.pyz` row. Each launcher should report `wfc 0.4.7`. Put upgrades in a new directory and retain the old one until its active workers have finished.
