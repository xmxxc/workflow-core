# OpenCode 2.x Windows 复测：WFC v0.4.7-rc.2

下载 [Windows ZIP](https://github.com/xmxxc/workflow-core/releases/download/v0.4.7-rc.2/wfc-v0.4.7-rc.2-windows-x64.zip) 和 [SHA256SUMS](https://github.com/xmxxc/workflow-core/releases/download/v0.4.7-rc.2/SHA256SUMS)，先比对：

```powershell
Get-FileHash .\wfc-v0.4.7-rc.2-windows-x64.zip -Algorithm SHA256
Get-Content .\SHA256SUMS
```

解压后 `wfc.cmd --version` 应显示 `wfc 0.4.7-rc.2`，包内 `COMMIT.txt` 记录构建源码。需自行安装 Python 3.9+、Herdr 0.9.5 和已登录的 OpenCode。

在新目录解包，下面假设 `C:\Tools\WFC candidate\wfc.cmd`。使用测试专用 endpoint 和 WFC_STATE，不复用旧 dispatch。先建工作目录、报告目录与 launch.json：

```powershell
$work = Join-Path $env:TEMP 'wfc-opencode2-candidate'
New-Item -ItemType Directory -Force "$work\reports" | Out-Null
$env:WFC_STATE = "$work\state"
$env:HERDR_SOCKET_PATH = 'wfc-opencode2-candidate'
$launch = @'
{"hosts":{"opencode":{"command":["D:/nodejs/node_modules/@opencode/cli/bin/opencode.exe"]}}}
'@
[IO.File]::WriteAllText("$work\launch.json", $launch, [Text.UTF8Encoding]::new($false))
@'
请先通过 brief 提供的 wfc ask 回调询问 WFC_WINDOWS_READY，然后结束本轮等待回答。收到 ready 后写报告，包含 WFC_WINDOWS_PASS，再通过 brief 的 done 回调完成。只操作此临时工作目录。
'@ | Set-Content -Encoding utf8 "$work\task.md"
# 在单独终端用实际 Herdr 路径启动同一 endpoint：
# $env:HERDR_SOCKET_PATH = 'wfc-opencode2-candidate'; & 'D:\实际路径\herdr.exe' server
$wfc = 'C:\Tools\WFC candidate\wfc.cmd'
& $wfc dispatch --backend herdr --herdr-endpoint $env:HERDR_SOCKET_PATH --host opencode --cwd $work --task "$work\task.md" --report "$work\reports\result.md" --launch-config "$work\launch.json"
& $wfc wait --timeout 90
# 不人工按 Enter。收到 question 后，用实际消息编号回复：
& $wfc reply <question编号> ready
& $wfc wait --timeout 90
```

记录 dispatch、attempt、OpenCode 版本、报告和未补按 Enter 的结果。若仍 stalled，保留终端画面和 status；不要把人工补键后的结果记为自动通过。

自动发现与 resume 单独复测：对**该测试自有** OpenCode 终端正常退出，使用 `status --dispatch <dispatch>` 确认停止，首轮已经 done，因此要带下一轮任务与新的报告路径恢复：

```powershell
'写报告包含 WFC_RESUME_PASS，并按新 brief 的 done 回调完成。' | Set-Content -Encoding utf8 "$work\task2.md"
& $wfc resume <dispatch> --task "$work\task2.md" --report "$work\reports\resume.md"
& $wfc wait --timeout 90
& $wfc status --dispatch <dispatch>
```

检查输出、报告和终端确实恢复同一 OpenCode 会话，且没有 `no such table: session`。遇到 uncertain 时先检查身份与状态，按诊断显式 reconcile，不能重复派发掩盖结果。

relay 沿用已有支持 `queue --resume` 的桌面 Codex 路径配置。先验首次无人工 Enter 的 ask/reply/done，再用 continue 下达延时任务、运行 relay，并核对同聊天消费与 ack。**5 分钟延时单独记录**；此前 Windows 报告只有 15 秒。此次候选的 Windows Codex worker、崩溃恢复、Herdr 重启、owner 接替和并发故障仍待实机验证。

完成复测后执行 `close <dispatch>`，停止专用 Herdr，保留测试目录和报告。
