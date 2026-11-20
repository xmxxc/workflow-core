# Windows 便携分发与验收

这是 #10 的 Windows 预发布测试版，版本为 0.4.7-rc.4。它包含 #30 的 Codex 回复提交修复和 #31 的 scoped detached relay 修复；短复测见随包的 `CODEX-Windows.md`（[源码版](verification/windows-codex-rc4.md)）。原生 Windows 固定候选的无人值守 Codex A 与 owner 接替后完成 D 仍待验收。[rc.2 实测记录](https://github.com/xmxxc/workflow-core/blob/b361b9e0e2447063515a57cb23e466f414cbf102/docs/verification/windows-opencode-rc2-20261115.md)记录 OpenCode 自动首派、问答、完成、同会话 resume、continue 和 300.011 秒 relay。包内保留 `OPENCode-Windows.md` 与 `CLOSE-Windows.md` 的历史步骤，重测时将其包路径和版本检查改为 rc.4。默认后端仍为 tmux；Windows 派发显式选择 `--backend herdr`。

## 下载

从 [v0.4.7-rc.4 发布页](https://github.com/xmxxc/workflow-core/releases/tag/v0.4.7-rc.4) 下载 `wfc-v0.4.7-rc.4-windows-x64.zip`，解压后在目录内使用 `wfc.cmd`。`SHA256SUMS` 提供校验和，`COMMIT.txt` 记录构建源码；此 ZIP 包含 WFC Python payload 与启动入口，依赖按下文单独安装。

## 安装与启动

依赖：原生 Windows x64、Python 3.9+、用户单独安装并启动的 Herdr 0.9.5（protocol 22），以及用户已登录的 Codex 和其需要的 Node runtime。CI 使用 Python 3.9/3.12 检查平台层，Herdr 合同使用 Python 3.12。真实 Windows Codex 版本待实机验收记录，不能把受控宿主版本当作 Codex 版本。安装 Python 时让 `python --version` 能从当前终端找到正确解释器。

从同一源码构建，无 WSL、shebang 或 executable bit 前提：

```powershell
python build.py --windows-dir 'C:\Tools\WFC candidate'
& 'C:\Tools\WFC candidate\wfc.cmd' --version
& 'C:\Tools\WFC candidate\wfc.cmd' --help
& 'C:\Tools\WFC candidate\wfc.cmd' status
```

cmd 对应入口：

```bat
"C:\Tools\WFC candidate\wfc.cmd" --version
"C:\Tools\WFC candidate\wfc.cmd" status
```

目录包含同源 `wfc.pyz`、`wfc.cmd` 和 LICENSE。wrapper 使用 PATH 上的 `python`，不修改全局 PATH 或 shell 配置。也可直接用指定解释器 `C:\Python312\python.exe C:\Tools\WFC\wfc.pyz --version`，路径有空格时按当前 shell 引用。Python payload 的 CLI 和内部子进程始终使用 argv；shell wrapper 的调用参数仍受 cmd/PowerShell 自身语法约束。

每项实测必须同时隔离 Herdr endpoint、配置和状态，以及 WFC 状态；只换 pipe 名仍可能共享默认布局。以下在专用 PowerShell 内运行（替换实际安装路径），Herdr 以子进程继承这些进程级变量：

```powershell
$work = Join-Path $env:TEMP ('wfc-local-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $work | Out-Null
$env:HERDR_SOCKET_PATH = Split-Path $work -Leaf
$env:HERDR_CONFIG_PATH = "$work\herdr.toml"
$env:XDG_CONFIG_HOME = "$work\config"
$env:XDG_STATE_HOME = "$work\herdr-state"
$env:XDG_DATA_HOME = "$work\data"
$env:XDG_CACHE_HOME = "$work\cache"
$env:WFC_STATE = "$work\wfc-state"
'[terminal]', 'default_shell = "cmd.exe"' | Set-Content -Encoding ascii $env:HERDR_CONFIG_PATH
$server = Start-Process -FilePath 'D:\Tools\herdr.exe' -ArgumentList 'server' -PassThru
# 待服务就绪后；task/report/cwd/launch.json 使用实际绝对路径：
& 'C:\Tools\WFC candidate\wfc.cmd' dispatch --backend herdr --herdr-endpoint $env:HERDR_SOCKET_PATH --host codex --task 'C:\Work\任务.md' --cwd 'C:\Work' --report 'C:\Work\reports\报告.md' --launch-config 'C:\Work\launch.json'
```

[Herdr 后端说明](herdr-backend.md) 包含归属、超时、恢复和权限边界。[启动配置](host-launch.md) 包含 Node/JS argv 配置。npm `codex.cmd` 不能作为直接宿主执行入口，配置实际 JS 脚本和 Node exe。不要用 CI 的测试下载工具作为依赖安装器；它只管理隔离的校验固定版本测试 fixture。

## 升级与卸载

升级到新的**空目录**并运行 `--version`、`status` 检查，再切换终端调用路径。构建拒绝覆盖已有安装目录。活跃 worker 依赖原 payload 路径，待其完成并显式回收后再删除旧目录。数据库按 `$env:WFC_STATE` 或 `%LOCALAPPDATA%\workflow-core` 独立保存；升级不清空状态。

卸载先检查当前 dispatch、未决操作和归属，完成或显式停止自有 worker；停止用户单独运行的 Herdr 服务后删除便携目录即可。数据库包含审计记录，按保留需求单独备份/删除。WFC 没有注册服务、全局 PATH 或后台自动更新，不卸载用户的 Python、Herdr 或 Codex。

空格和 Unicode 路径由原生 CI 验证；常见本地盘符随 Windows runner 路径覆盖。UNC 和网络共享上的状态锁、ACL 与终端 cwd 未验证，暂不列入支持范围，应使用本地磁盘。不要从 Windows 结果推导 Unix 后端迁移；Unix tmux 继续保留。

## 可重复证据与待验收项

CI 执行源码和打包 payload 两套真实 Herdr 合同，受控 Python 宿主覆盖派发、ask/reply/done/continue、interrupt/close/resume、迟到 attempt、owner 接替、只读拒绝、共享 pane 安全回收与服务重启。Unix 丢响应 proxy 另覆盖 uncertain 后不重放；Windows 命名管道版本不声称覆盖该故障注入。打包测试逐字节核对模块与源码，并从 cmd 和 Windows PowerShell 启动 Unicode/空格目录中的入口。

真实 Windows 验收需记录 OS/机器、候选 commit、Python/Herdr/Codex 版本和脱敏后的逐步结果，完成：真实宿主派发→问题唤醒→回答→done→continue→退出/resume→接替→回收，以及重复请求、worker 崩溃、router/Herdr 重启、权限拒绝、未知输入、关闭启动终端。验证无双 writer、消息确认和显式 reconcile，最后由用户核对。该清单的未验项目保持 pending，不以自动合同代替实机结果；发生缺陷保留可复现证据，另开具体问题。
