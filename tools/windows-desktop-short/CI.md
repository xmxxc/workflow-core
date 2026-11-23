# Windows 原生采集验收

本包供总控复核。用 windows-2022 x64、Python 3.12 和原始 2409c40 内层 Windows ZIP。该包来自作业 44753578583 的工件 14636493473，内层 ZIP 的 SHA-256 为 `ff7bf5a0fe76297d4ef6040f01bdd7065474a1591af484aff9e60b2965e059ee`。测试通过原包 pyz 和 kernel 运行合成 worker，使用 Windows Job 与 ConPTY（原生进程组与终端）。无需账户、模型或网络请求。

1. 分别为 `early` 和 `worker` 建立独立作业，矩阵设 `fail-fast: false`。下载并校验冻结 ZIP，解压至新目录，执行下面的命令。两个作业各自使用新的试验路径。退出码须为 0。
2. 每个作业先跑入口校验和本包离线测试，再从公开 dispatch 启动真实进程。early 要求进程实际退出 17、自然终屏与持久 stderr 均检出受控错误、预备参数与实际 Popen 审计一致，且公开 reply 为 0。worker 要求 worker 提问、回合结束后只收到一次公开 reply、闭后目标会话精确匹配一条、报告和 done 已处理。两路均须证实签名 close、四个创建身份退出、后台已停、waiter 为零和私有状态已删除。
3. 无论成功或失败，保留作业日志与退出码；仅收集各作业 `Trial/evidence/result.json` 以及存在时的 `inputs.jsonl`。early 在发信前退出，不会生成 inputs 文件。请勿上传 scratch、用户目录或原始进程环境。

```powershell
# $root 为本工具解压目录，$candidateZip 为已核验的候选内层 ZIP，$case 来自矩阵
& "$root/ci/run-native.ps1" -CandidateZip $candidateZip -DesktopTools "$root/desktop" -Case $case -Trial "$env:RUNNER_TEMP/wfc-r2-$case"
if ($LASTEXITCODE) { exit $LASTEXITCODE }
```

本版在诊断用 Python 进程内包裹原包 supervisor，观测 Popen 及产品写出的 started 回执。实际 worker 的 argv、环境、cwd、启动权限及终端读写沿用产品。该观测会增加少量启动耗时。两路 CI 的结果只能验证采集器在受控 Windows 上的行为。后续是否使用真实桌面的 `desktop/run.cmd` 由总控复核决定。
