# Windows 采集路径验证

在 windows-2022 上使用 Python 3.12 和 2409c40 的原始内层 Windows ZIP。解开本轮冻结的桌面工具至单独目录，在与真实 Codex 启动前检独立的作业或步骤中执行下面命令。两个步骤可并行，前检的失败不应跳过采集验证。

```powershell
& .\desktop-ci\run-native.ps1 -CandidateZip $candidateZip -DesktopTools $unpackedDesktopTools -Trial "$env:RUNNER_TEMP\desktop-collector-fixture"
```

该作业先校验候选 ZIP、脚本使用的包入口及采集器的反例，然后在真实 Windows Job 和 ConPTY 下发送一次公开 WFC reply。worker 由受控 fixture 实现，使用隔离的临时 session index/rollout 和真实 WFC ask/done。验收要求同一本轮闭后规范消息精确命中一条、输入哈希与输出一致、done/ACK、Job 关闭及创建身份退出。试验结果标注为 native_collector_fixture。

CI 用 always 收集 Trial/evidence 下的 result.json 和 inputs.jsonl，并保存退出码和作业日志。若失败，保留该次现场。已冻结桌面包只在这条采集链核验完成后作为待实机短测的工件转交。
