# Issue #17 最终候选复核

日期：2026-11-14。PR #18 基于已集成基线 `cc12a8dee35c3d72bd76e5b5db8fc2cb72a5a968`。

## 候选与归属

原 PR 候选为 `b96fedfb08d22adfadd99799402f8af4c562ebb3`。在原实施工作树发现三份已有未提交 r2 修复：`host_launch.py`、`tests/test_host_launch.py`、`docs/host-launch.md`。将其冻结后复制到隔离工作树做最终复核，保留原工作树、报告与事故状态。新增修改仅为受控终端测试的确定性启动同步，以及本验证记录。

最终审查产品与测试的 SHA-256：

- `router.py`：`8d0b41a993e1d78217f7c8a032cf2684a06236cac85f591554fc4b94707db99f`
- `host_launch.py`：`e029d9416cbc5a31c11462ec733addfacf7261d71015763e87ea44810932ee01`
- `tests/test_host_launch.py`：`34d961460f3c90f04dccc219e7fd906742befdd1d815c2c4b9aa13751e54d99f`
- `docs/host-launch.md`：`af3148b1aae6fa5cfe4eaac2ecec9497feaae9665f353899db9b22848e9de909`

## 修复与边界

原独立审查 P1 是 stderr 字节采集在 UTF-8 或凭据中间截断，导致已知凭据前缀进入持久诊断。r2 在 decode 前对原始字节执行遮罩，将完整值、截断前缀与重叠区间合并后替换，再做语法去敏及最终字符数限制。保持原有 8192-byte 采集上限、4096-character 诊断上限和 owner/attempt、unknown 对账语义。Unix 内部实现断言改为跨进程 CLI/status 验证；Windows 适配模拟独立标记。

最初复跑受控启动测试遇到一次 Codex 未在 400ms 确认启动，另一次旧测试运行的 resume 子场景也失败；保留 `/tmp/wfc-18-focused.log` 与 `/tmp/wfc-18-full-tests.log`。受控 tmux 现在等待其子进程的原子 receipt 后才返回，最多五秒，显式 `NO_HOST_ACK` 场景仍不启动子进程。两项对应测试定向复跑通过，6.800 秒。产品启动观察窗口和 unknown 行为没有改变。旧 full suite 在更新 fixture 后明确停止，最终 suite 重新执行，不将旧未完成运行计为通过。

## 独立审查与执行证据

两位独立审查者消费同一完整 base 到最终候选的冻结 diff，分别检查 Standards 与 Spec。双方对 fixture 同步后的最终快照复核通过：Standards 0 项，Spec 0 项。

Spec 审查者独立执行五项跨进程用例：UTF-8/长凭据/重叠遮罩、JSON 编码截断、大量 stderr、unknown 启动对账、resume/handoff 与迟到 attempt，全部通过，31.941 秒。源码审查不等同于独立完整 suite。

实施会话额外验证：

- 五个既有审查探针在最终候选重跑，通过，6.260 秒；覆盖 shell 字符作为 literal argv、保留 router 身份、旧 receipt 隔离、unknown/owner fencing、私有权限、相对 PATH 和旧 schema。
- 最终 zipapp 的四组长 Unicode/ASCII 及多字节上下文 canary 探针全部通过：持久 receipt 中凭据字符数均为 0，状态诊断与 receipt 一致，退出码 23 保留。
- `python3 -m py_compile router.py host_launch.py wfc.py build.py`、`git diff --check`、构建 zipapp 及 `--version`/`--help` 通过。

## 真实宿主与平台限制

复用原实施会话保留的真实 Codex tmux 探针，不将其伪称为最终 r2 的新实机执行：原候选构建物 `d-8c3305b6` / attempt `a-60d6fbd2bcb4` 完成，Node v20.19.6；首次探针 TERM=dumb 的退出失败也保留。后续修改仅覆盖诊断遮罩与测试，真实宿主启动 argv/环境路径没有变化。原证据位置为 `docs/agents/work/ai-coding/host-launch-17/reports/17-probe/`（原工作区保留）。

本次环境为 Linux，没有原生 Windows/Herdr 闭环验收。Windows exe/cmd 单测仅为适配边界模拟，不能替代 #8/#9/#10。原事故 relay 未启动；不合并、发布或关闭 issue。

最终完整验证：`python3 -m unittest discover -s tests -v` 共 **241 项全部通过**，437.023 秒；日志 `/tmp/wfc-18-full-final.log`。最终源码哈希与上述冻结身份一致。远端 CI 必须针对追加提交重新执行，不复用原候选绿色结果。
