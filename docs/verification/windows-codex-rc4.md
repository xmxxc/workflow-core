# Windows Codex rc.4：A / D 短复测

从 rc.4 ZIP 解压，校验同页 `SHA256SUMS`，保留 `COMMIT.txt`。以下命令仅用于新的测试目录；A、D 各重建一套。记录 OS、commit、Python、Herdr、worker Codex 与 Desktop queue CLI 版本。WFC 只打包 router，不包含宿主、登录凭据或 Desktop CLI 的自动发现。准备已登录的原生 Codex/OpenCode，保留各自现有登录配置；不要复制凭据或编辑数据库。

## 隔离环境与实际安装路径

由**目标 Desktop controller 聊天自身的 PowerShell 工具环境**执行。先把以下路径改为本机实际安装路径：worker 使用实际 JS + Node（或按 `host-launch.md` 改为原生 exe），OpenCode 使用原生 exe；不能把 npm `.cmd` shim 写进 launch.json。Desktop queue 使用另外的原生 `codex.exe`，须支持 `queue --resume`；npm Codex 0.164 不支持该命令。所有变量仅限本进程，不改全局 PATH/登录配置，也不改 `CODEX_HOME`。

```powershell
$wfc = 'C:\Tools\wfc-rc4\wfc\wfc.cmd'
$herdr = 'D:\Tools\herdr.exe'
$node = 'D:\nodejs\node.exe'
$codexJs = 'D:\nodejs\node_modules\@openai\codex\bin\codex.js'
$opencode = 'D:\nodejs\node_modules\@opencode\cli\bin\opencode.exe'
$desktopCodex = 'C:\Tools\DesktopCodex\codex.exe'
foreach ($p in @($wfc, $herdr, $node, $codexJs, $opencode, $desktopCodex)) {
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "Set actual installed path: $p" }
}
& $wfc --version # 必须为 wfc 0.4.7-rc.4
python --version
& $herdr --version # 必须为 0.9.5
& $node $codexJs --version
& $desktopCodex --version
& $desktopCodex queue --help # 确认 --resume，不能向任意线程试投递
if (-not $env:CODEX_THREAD_ID) { throw 'Run from the intended Desktop controller chat' }
$controllerThread = $env:CODEX_THREAD_ID # 保留真实 thread，不手填或伪造
$env:PATH = (Split-Path $desktopCodex -Parent) + ';' + $env:PATH
if ((Get-Command codex -CommandType Application).Source -ne $desktopCodex) { throw 'Wrong queue CLI on process PATH' }

$work = Join-Path $env:TEMP ('wfc-rc4-A-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force "$work\reports" | Out-Null
$scope = Split-Path $work -Leaf
$env:WFC_STATE = "$work\wfc-state"
Remove-Item Env:WFC_CONTROLLER_TOKEN, Env:WFC_OWNER_TOKEN, Env:WFC_DISPATCH -ErrorAction SilentlyContinue
$env:HERDR_SOCKET_PATH = $scope
$env:HERDR_CONFIG_PATH = "$work\herdr.toml"
$env:XDG_CONFIG_HOME = "$work\config"
$env:XDG_STATE_HOME = "$work\herdr-state"
$env:XDG_DATA_HOME = "$work\data"
$env:XDG_CACHE_HOME = "$work\cache"
'[terminal]', 'default_shell = "cmd.exe"' | Set-Content -Encoding ascii $env:HERDR_CONFIG_PATH
$launch = @{ hosts = @{
    codex = @{ command = @($codexJs, '--no-daemon'); runtime = @($node); env = @{ TERM = 'xterm-256color' } }
    opencode = @{ command = @($opencode) }
} } | ConvertTo-Json -Depth 6
[IO.File]::WriteAllText("$work\launch.json", $launch, [Text.UTF8Encoding]::new($false))
$server = Start-Process -FilePath $herdr -ArgumentList 'server' -PassThru
$server | Select-Object Id, StartTime, Path # 保留测试服务身份；就绪后再派发
```

新 XDG 目录也可能改变宿主配置查找；事先核对宿主仍使用其已授权登录配置，必要时用 launch.json 的宿主 `env` 显式指定现有配置路径。启动失败或登录提示先修复配置，不能记为 WFC 验收成功。只换 endpoint 不隔离配置/状态会共享布局。

## A：无人值守 ask → reply → done，同聊天 detached relay

这是未认领的新 scope：不运行 owner claim，不传 `--owner-token`。准备任务后运行原始公开命令：

```powershell
@'
Only use this scratch directory. Invoke the exact ask command from the WFC brief once, asking WFC_RC4_READY?, then end the turn. On the router answer WFC_RC4_GO, write WFC_RC4_PASS to the assigned report, invoke the exact done command from the brief, and end the turn. Never ask a second time; on resume check what already happened first.
'@ | Set-Content -Encoding ascii "$work\task.md"
$started = & $wfc dispatch --backend herdr --herdr-endpoint $scope --host codex --scope $scope --cwd $work --task "$work\task.md" --report "$work\reports\result.md" --launch-config "$work\launch.json" | ConvertFrom-Json
$d = $started.dispatch
$relay = & $wfc relay --codex --scope $scope --for 180 --stall-after 180 --detach | ConvertFrom-Json
& $wfc status --dispatch $d
```

controller 结束当前 turn，让 relay 将 question 送为**同一个 Desktop 聊天的新 turn**。不要同时用 wait 抢消息。下一 turn 恢复相同 `$work`、`WFC_STATE`、scope、进程 PATH；`CODEX_THREAD_ID` 必须仍等于记录的 controller thread。处理 question 时，用收到 envelope 的实际数值 `id` 作为 `$questionId`、稳定 `messageId` 作为 `$questionMessageId`，只 reply 一次：

```powershell
& $wfc reply $questionId 'WFC_RC4_GO'
& $wfc ack $questionMessageId
```

结束 turn，等 done 再唤醒同一聊天；实际检查报告后，用 done envelope 的 `messageId` 作为 `$doneMessageId`：

```powershell
Get-Content "$work\reports\result.md"
& $wfc ack $doneMessageId
& $wfc status --dispatch $d
```

A 全部满足才通过：全程无人工 Enter/补键；同一 worker 宿主的规范会话记录恰好一条完整 `[router] Answer to question <id>: WFC_RC4_GO` user 消息（屏幕文字不算入队证明，重复投影事件不重复计数）；ask/reply/done 身份及 report 一致；relay 的原 envelope `dispatch/scope/messageId/round/attempt/report/statusVersion/deliveryAttempt` 与本地记录对应，pending/unknown 发送态之后推进 received，实际处理后 ACK 为 processed。保留 child PID、创建身份、`kind: relay` 注册与日志，180 秒后核对该身份进程已退出、waiters 无该 child。CLI exit 0 或屏幕出现文本单独均不够。

## D：owner fencing 与恢复完成分别记录

独立重跑上面的环境准备，将目录前缀改为 `wfc-rc4-D-`；不要复用 A 的状态、endpoint 或 Herdr 布局。沿用相同任务和 dispatch 命令，但先认领 scope：

```powershell
$oldOwner = & $wfc owner acquire --scope $scope --owner rc4-D-old --lease 600 | ConvertFrom-Json
$env:WFC_CONTROLLER_TOKEN = $oldOwner.token
# 此时执行上面的任务准备与 dispatch，worker 继承旧 owner；记录其 attempt/宿主会话身份。
# 等 worker ask 后再接替；此项可用 wait --scope $scope --timeout 90 取 question。
$newOwner = & $wfc owner takeover --scope $scope --owner rc4-D-new --lease 600 | ConvertFrom-Json
$env:WFC_CONTROLLER_TOKEN = $newOwner.token
# 使用实际 question id，只 reply 一次；无人工补键，核对原 worker 恰好一条 user 回复。
& $wfc reply $questionId 'WFC_RC4_GO'
& $wfc status --dispatch $d
```

先单独记录 generation 增加及旧 worker 用**自身原凭据**调用 brief 的 done 得到 `stale_controller_owner`。这是预期拒绝，不能当作接替完成，也不能把新 token 塞给旧 worker。随后核对原 worker 及其 supervisor 的 PID、创建身份、命令行、backend handle 与宿主会话，只停止这份测试的旧 writer，确认完全退出；不能只关闭可见窗口或凭 PID 猜测。

在当前 owner 下执行 `resume $d`；如返回 `lifecycle_reconcile_required`，先保存诊断，用 `status` 找到**本 generation** 的 pending/uncertain audit。只有证据确认旧 writer 消失后，才填写实际 audit ID 和证据路径（不是照抄未知 ID）：

```powershell
& $wfc reconcile $d $auditId --result unknown --actor rc4-D-new --evidence $oldWriterGoneEvidence
& $wfc resume $d
& $wfc status --dispatch $d
```

仅对实际存在的未决 audit 对账；如 resume 已确认旧 writer 消失并成功，则直接检查新 attempt。unknown 不授权重放；若 backend 仍不能确认消失而再次拒绝，停下保留证据，不循环 reconcile/resume。不得跨 generation 对账旧 owner 的 audit 或修改数据库绕过拒绝。

确认新 attempt 使用当前 owner、恢复**同一宿主会话**，worker 根据已有报告/回复完成原任务，新 done 被 controller 实际处理并 ACK processed，旧 attempt 迟到调用仍被拒绝；此时才记录 D 恢复完成。仍被 fenced 或无法安全 resume 时保持 pending，另属 #4/#6 的恢复评估。

## 留证与清理

保留脱敏后的命令结果、版本/commit、host user 消息计数、消息 ID/版本、controller thread、child 创建/注册/退出、owner generation、前后 attempt/宿主会话身份。不要上传 owner token 或含凭据数据库。完成后 close 自有 worker，核对退出，再按记录的身份停止测试专用 Herdr；relay 等待有界退出并核对，不停止其他 scope 的服务。

rc.3 历史结果：A 人工 Enter + 前台 relay 后同聊天/processed；B 退出/原会话恢复、C unknown 后核实旧进程结束再对账/resume 有条件通过；D 观察到 generation/旧凭据拒绝，完成路径未验。本 rc.4 的原生 Windows A/D 仍 pending；Linux 回归、真实 Herdr 和 Linux Codex 的单消息/同聊天 ACK 证据不替代 Windows。五分钟独立计时、全部 Windows worker、崩溃/Herdr 重启及并发恢复矩阵也仍 pending。
