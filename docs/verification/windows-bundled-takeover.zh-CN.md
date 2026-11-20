# Windows bundled 双聊天接替验收

本用例检查 B 接替后，A 派出的同一个 worker 能否向 B 提问并交付报告。记录三份独立结果：Windows 受控源码、搬迁后的 Windows 包、Desktop 两个真实聊天。最后一项需要人工在专用测试聊天中执行。

## 先在 Windows CI 验证候选

使用既有 bundled 原生作业构建的 Windows companion 和同一候选源码。以下命令从仓库根目录执行，fixture 使用自己的临时状态、受控 PTY worker 和 Job。每个阶段必须退出 0，并出现候选的 `TAKEOVER` 记录。保存 commit、Python 版本、pyz/EXE 的 SHA-256 及清理结果。原生套件如整体跳过，应停下检查 companion 设置。

```powershell
$env:PYTHONUTF8 = '1'
$env:WFC_BUNDLED_TEST_KERNEL = (Resolve-Path $candidateKernel).Path
Remove-Item Env:WFC_TEST_PROGRAM,Env:WFC_TEST_DOT3_BASELINE,Env:WFC_TEST_DOT3_LAUNCH,Env:WFC_BUNDLED_TEST_DISCOVERY -ErrorAction SilentlyContinue
python -m unittest discover -s tests -p test_bundled.py -v
if ($LASTEXITCODE) { throw '源码验证失败' }

$pkg = Join-Path $env:RUNNER_TEMP ('搬迁 包 ' + [guid]::NewGuid())
python build.py --windows-dir $pkg
if ($LASTEXITCODE) { throw '构建失败' }
Copy-Item $env:WFC_BUNDLED_TEST_KERNEL "$pkg\wfc-kernel.exe"
$env:WFC_TEST_PROGRAM = "$pkg\wfc.pyz"
$env:WFC_BUNDLED_TEST_KERNEL = "$pkg\wfc-kernel.exe"
$env:WFC_BUNDLED_TEST_DISCOVERY = '1'
Get-FileHash $env:WFC_TEST_PROGRAM,$env:WFC_BUNDLED_TEST_KERNEL
python -m unittest discover -s tests -p test_bundled.py -v
if ($LASTEXITCODE) { throw '搬迁包验证失败' }
```

下载包复测时，将同一次候选 CI 工件解压至新的含空格路径，将上述第二阶段的两个文件变量指向解压件，核对清单和文件哈希，再运行同一套件。正式 `.3` 的预期结果不同：已在途的旧 worker 携带 A 的 owner 凭据。B 接替后，原 worker 的 ask/done 均退出 1，错误为 `stale_controller_owner`，没有生成问题或完成回执。此时后端仍可能显示 `online`，轮次仍为 `running`。

## 准备两个聊天

在同一 Windows 测试账号开两个独立顶层聊天 A、B，记录各自的 `CODEX_THREAD_ID`，两者必须不同。选取全新测试目录、新 scope 和已校验的候选包。两个聊天使用同一份 WFC 程序及状态目录，分别在自己的聊天内启动 relay。使用已验证可非交互启动的 Codex host 配置和已信任的测试 worktree。若本机入口为批处理脚本，沿用现有 Windows 实测的显式 runtime/host 启动配置。

在每次 PowerShell 调用中恢复相同的上下文。用本次的实际绝对路径替换下面的示例。仅第一次建目录，若目录已经有 router.db，另取一个新目录。

```powershell
$wfc = 'C:\trial\candidate package\wfc.cmd'
$trial = 'C:\trial\D-unique-run'
$scope = 'D-unique-run'
$env:WFC_STATE = "$trial\state"
$work = "$trial\work"
$reports = "$trial\reports"
# 首次在 A 执行
New-Item -ItemType Directory -Force $work,$reports,"$trial\control" | Out-Null
```

owner 命令返回的 JSON 包含凭据。直接赋给变量，仅输出 scope、owner、generation、leaseUntil。跨 shell 调用时可用本账号的 Windows DPAPI 分别保存 A、B 的测试凭据，放在 worktree、报告和共享证据目录之外。不要复制给 worker。后续每条 controller 命令在本次 PowerShell 调用内设置自己那一代的环境变量。例如：

```powershell
# 每个聊天分别设置 $role 为 A 或 B
$tokenFile = Join-Path $env:LOCALAPPDATA ("wfc-" + $scope + "-" + $role + ".credential")
# 刚完成 acquire/takeover 时保存，$owner 是本次命令解析的结果
(ConvertTo-SecureString $owner.token -AsPlainText -Force) | ConvertFrom-SecureString |
    Set-Content -LiteralPath $tokenFile
# 后续调用重新加载
$protected = (Get-Content -Raw -LiteralPath $tokenFile).Trim() | ConvertTo-SecureString
$env:WFC_CONTROLLER_TOKEN = [System.Net.NetworkCredential]::new('', $protected).Password
```

### A 派发，worker 等待放行

1. 在 A 执行下列命令认领并启动 A 的 relay。记录 relay 的 PID、创建时间、本聊天 ID 和 `status.waiters` 中的注册记录。
2. 准备任务文件，要求 worker 在自己的 worktree 写入 `ready.txt`，内容为生成 brief 给出的 dispatch/attempt。然后在同一轮用一次有界 PowerShell 等待 `release.txt` 出现，最多 240 秒。超时退出等待并停止本次验收。看到放行文件才用原 brief 的 ask 命令提出唯一问题 `D_GO?` 并结束 turn。收到 `D_GO` 后写指定报告，再按原 brief 运行 done。
3. 使用测试 host 配置派发一次。将 dispatch、round、attempt 写入公开测试记录供 B 核对。等 `ready.txt` 出现，此时应有 `status=online`、`lifecycle=running`、`backendAlive=true`，且 messages 中尚无 question 或 done。保存 launch PID/processStart、backendHandle.generation、轮次及 worker 宿主会话身份。到此暂停 A 的业务操作。

```powershell
$owner = & $wfc owner acquire --scope $scope --owner A --lease 900 | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'A 认领失败' }
$env:WFC_CONTROLLER_TOKEN = $owner.token
$owner | Select-Object scope,owner,generation,leaseUntil
& $wfc relay --codex --scope $scope --for 300 --detach
# 将步骤 2 的任务保存为 $work\task.md 后执行
$job = & $wfc dispatch --backend bundled --host codex --cwd $work --scope $scope --task "$work\task.md" --report "$reports\result.md" --launch-config $launchConfig | ConvertFrom-Json
if ($LASTEXITCODE) { throw '派发失败' }
$d = $job.dispatch
& $wfc status --dispatch $d
```

### B 接替，随后放行同一 worker

1. B 确认 A 的 ready 证据和 scope，再 takeover 一次。B 的 generation 应比 A 大 1。保留同一 dispatch、attempt、宿主 PID/创建身份及 backend generation 的前后记录。B 在自己的聊天中注册 relay 并确认在 waiters 中出现。A 的旧 relay 应退出或记录 stale owner。任何身份不符、未决外发或 relay 注册失败，先停在 gate。
2. A 用保存的 A 凭据执行 `wait --scope $scope --timeout 0` 和 `nudge $d --text D_STALE_PROBE`。预期两次均非零、`stale_controller_owner`，宿主没有该输入。不要认领回 A。
3. B 在 worktree 写 `release.txt` 后结束自己的 turn，等 relay 唤醒。两个聊天均保留消息记录。新 question 应只进入 B。同一 `messageId` 在账本已经 `received` 后，B 用实际数字 question id 执行一次 `reply`，确认处理后 ACK。worker 正常继续并向 B 回报 done。验收期间不另开 wait 消费 B 的批次。

```powershell
$owner = & $wfc owner takeover --scope $scope --owner B --lease 900 | ConvertFrom-Json
if ($LASTEXITCODE) { throw '接替失败' }
$env:WFC_CONTROLLER_TOKEN = $owner.token
$owner | Select-Object scope,owner,generation,leaseUntil
& $wfc relay --codex --scope $scope --for 300 --detach
& $wfc status --dispatch $d
# 完成注册及 A 拒绝检查后
Set-Content -LiteralPath "$work\release.txt" -Value D_GO
# B 被 question 唤醒后
& $wfc reply $questionId D_GO
& $wfc ack $questionMessageId
```

B 收到 done 后先读取报告并核对内容、路径、dispatch/round/attempt。A 先用自己的旧凭据 ACK 该 done，必须非零且账本仍为 received。B 随后 ACK，得到 processed；B 重复同一 ACK 应为 duplicate 且版本不变。记录两个聊天收到的消息数、稳定 messageId、statusVersion、宿主规范 user 消息里恰好一次答复和全程 PID/创建身份。要求 A 在接替后的新问题、完成消息均为零；B 对每项各收到一次。屏幕回显、仅有报告文件或仅显示 completed 都不足以完成验收。

### 外部队列与停止条件

scoped relay 先将批次提交为 unknown，再调用外部队列。候选在 scope owner 锁内完成这次调用及结果记账；B 的 takeover 成功返回后，A 的下一次领取会被拒绝。已经被外部队列接受的消息仍可能稍后显示在 A。进程退出、超时或回执不确定时，保留 unknown；B 核对外部聊天和本地消息身份，有独立送达证据才按现有 resolve 流程对账。接替等待 owner 锁超时、队列结果不明、问答重复、attempt 变化、A 收到放行后的消息，或缺少可核实的宿主身份，均停止本轮。不要补发消息、自动重派或反复 takeover。

旧版在途 worker 的凭据不能转换为本次 callback。B 可检查原报告和回执并记录阻塞。若需恢复执行，先确认旧 writer 的归属、完成正常 close 或取得确切退出证据，再由 B 显式 resume；未决 lifecycle 按已有审计和证据处理。新 attempt 须单独核验，原 attempt 的同进程接替结果仍记为阻塞。保留已确认的工作，不重复执行未知副作用。

完成或停止后，由当前 owner 只 close 本次 dispatch。核对 backendAlive=false、测试 worker 及其 guardian/kernel 的创建身份已退出、两个有界 relay 到期退出且 waiters 为空。删除本次 DPAPI 凭据文件，保留脱敏记录；不上传数据库、私有启动 spec、完整环境或原始 owner JSON。本用例检验正常 CLI 的凭据和协作角色。owner takeover 是本地操作员显式可用的命令。能另起进程、重设环境或写入状态库的同账号程序处于受信任范围内；隔离不受信任的程序需要独立操作系统权限。
