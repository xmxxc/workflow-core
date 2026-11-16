# Windows 最小复测：close 保留完成结果（#28）

使用 rc.3 解压目录里的 `wfc.cmd`，确认 `--version` 输出 `wfc 0.4.7-rc.3`。使用已安装的 Herdr 0.9.5 和已登录的 OpenCode。只用一份新的测试状态和一个短任务；不用等待五分钟，也不用问答、resume 或 relay。不要用旧状态库或已有任务。

在一个 PowerShell 启动专用 Herdr（替换成实际 exe 路径），保持终端运行：

```powershell
$env:HERDR_SOCKET_PATH = 'wfc-rc3-close-check'
& 'D:\Tools\herdr.exe' server
```

另开 PowerShell。替换 WFC 与 OpenCode 的实际路径，派发一个只写报告并回调 done 的任务：

```powershell
$wfc = 'C:\Tools\wfc-rc3\wfc\wfc.cmd'
$opencode = 'D:\nodejs\node_modules\@opencode\cli\bin\opencode.exe'
$work = Join-Path $env:TEMP ('wfc-rc3-close-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force "$work\reports" | Out-Null
$env:WFC_STATE = "$work\state"
$launch = @{ hosts = @{ opencode = @{ command = @($opencode) } } } | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText("$work\launch.json", $launch, [Text.UTF8Encoding]::new($false))
'Write WFC_RC3_CLOSE_PASS to the assigned report and invoke the done command from the brief, then finish the turn. Only use this scratch directory.' | Set-Content -Encoding ascii "$work\task.md"
$started = & $wfc dispatch --backend herdr --herdr-endpoint wfc-rc3-close-check --host opencode --cwd $work --task "$work\task.md" --report "$work\reports\result.md" --launch-config "$work\launch.json" | ConvertFrom-Json
$d = $started.dispatch
$batch = & $wfc wait --scope $started.scope --timeout 90 | ConvertFrom-Json
$done = $batch.messages | Where-Object { $_.type -eq 'done' -and $_.dispatch -eq $d } | Select-Object -First 1
if (-not $done) { throw 'No matching done message; keep the test state and inspect status before closing.' }
Get-Content "$work\reports\result.md"
& $wfc ack $done.messageId
$before = (& $wfc status --dispatch $d | ConvertFrom-Json).dispatches[0]
& $wfc close $d
$after = (& $wfc status --dispatch $d | ConvertFrom-Json).dispatches[0]
$before.rounds | ConvertTo-Json -Depth 8
$after.rounds | ConvertTo-Json -Depth 8
$before.messages | Where-Object { $_.messageId -eq $done.messageId }
$after.messages | Where-Object { $_.messageId -eq $done.messageId }
$after | Select-Object status, backendAlive, backendResult
$after.audit | Select-Object -Last 1
```

- **任务结果：**报告包含 `WFC_RC3_CLOSE_PASS`。关闭前后轮次均为 `completed`，`result` 和 `completed` 时间完全一致；相同 `messageId` 的回执仍为 `processed`，版本不变，任务 `status` 仍为 `completed`。
- **执行事实：**`close` 返回 `result: completed`，审计记录操作 `close`、结果 `completed`，`backendAlive` 为 false。若关闭返回 `uncertain`，保留状态与诊断，先对账再重试，不能记为资源清理成功。

结束后停止测试专用 Herdr，保留测试目录、前后状态和报告。
