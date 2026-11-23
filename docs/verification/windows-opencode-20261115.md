# Windows OpenCode 与 Codex controller 实测回传

来源：用户提供的 Windows 验证交接，更新于 2026-11-15（Asia/Shanghai）。以下 Windows 结果来自该实机报告，本仓库维护环境为 Linux；后续自动回归另行标记。

## 候选与环境

- WFC `v0.4.7-rc.1`；Windows ZIP SHA256 `b07e31c10ae713c85e44b523a6c103e624c47023523f68a1309dad570750f164`，报告确认与 SHA256SUMS 和 release digest 一致。
- 原生 Windows，Python 3.13.12、Herdr 0.9.5、OpenCode 2.0.12。Herdr 依赖 ZIP 本次未另做上游校验和比对。
- OpenCode 原生入口 `D:/nodejs/node_modules/@opencode/cli/bin/opencode.exe`，通过 launch.json 指定；依赖使用局部路径配置。
- relay 使用桌面 Codex `0.169.0-alpha.11.1`，仅对该进程调整 PATH。npm Codex 0.164.0 的 queue 不支持本次所需的 `--resume`。
- 独立状态目录为 `%LOCALAPPDATA%/Temp/wfc-windows-probe-20261115/state`，endpoint `wfc-codex-probe-20261115`；报告、launch.json、任务和 SQLite 审计均保留。

## 已观察闭环

Dispatch `d-969b8acd`，scope `s-c06dfe74`。

1. 首轮 attempt `a-8d1a237b6bcb`：真实 OpenCode 进程已启动；初始 brief 留在输入框，wait 回传 stalled。测试者检查终端后补一次 Enter，约 6 秒后收到 question `WFC_WINDOWS_READY`。`reply 2 ready` 后收到 done，`reports/result.md` 包含 `WFC_WINDOWS_PASS`。
2. `continue` 复用同一会话，attempt `a-5ea31d32b443`。延时 15 秒后写入 `reports/relay.md`，包含 `WFC_RELAY_PASS`，并提交 done。
3. `relay --codex --scope s-c06dfe74 --for 45` 返回 0，消息 6 变为 received。只读查询 Codex 队列确认该 done 已入当前聊天（别名 `thread-windows-opencode`）。
4. controller 上一回合结束后，桌面将原始 router 消息投递到同一聊天并触发新回合。controller 检查报告后执行 `ack m-6be20cf986864787a228459f33d999a9`，返回 `processed`、`statusVersion: 4`、`duplicate: false`。
5. `close` 返回 completed；专用 Herdr server 已停止，测试文件与状态保留。

已观察的 relay 结果包含入队、桌面实际消费、同一聊天续轮和处理确认，而不止是 CLI 退出码。

## 缺陷与待验证范围

- 首次 prompt 自动提交需要修复；不能将补键后的成功描述为全自动首派成功。
- 默认 OpenCode 数据库持续出现 `no such table: session`；显式 ask/done 成功，不等于自动会话发现、错误检测和 resume 已通过。
- 本次未覆盖 worker 崩溃、Herdr 重启、owner 接替或并发故障。原请求的 5 分钟延时尚未用该 Windows 包复测。
- Windows 真实宿主证据为 OpenCode worker + Codex controller；Codex 作为 Windows worker 的闭环仍单独待验。

## 适配依据

OpenCode 2.0.12 固定源码为 [2670273ff17da96f85c5826ced57aa1b368754fa](https://github.com/anomalyco/opencode/tree/2670273ff17da96f85c5826ced57aa1b368754fa)。`session_v2` / `session_message`、user 文本和结构化 error/idle 语义分别见 `packages/core/src/session/sql.ts` 与 `packages/schema/src/session-message.ts`、`session-error.ts`。初始 `--prompt` 的自动提交受 TUI model ready 与 composer 状态约束，见 `packages/tui/src/routes/home.tsx`。

## 本次修复的 Linux 验证

- 使用隔离目录与固定 OpenCode 2.0.12、Herdr 0.9.5 复现首次输入：修复前 user 消息为 0，人工 Enter 后为 1；修复后无需人工 Enter 已为 1，继续观察仍为 1。首次补键只接受两次稳定画面中含本次 attempt 的 brief 完整末尾；不重打文本。
- 真实 Herdr 合同覆盖首次自动提交、空/无关/重叠草稿不提交、blocked Enter 拒绝且可中断、并发 nudge 串行，以及原有身份隔离、丢响应不重放、重启未知结果等。12 项通过；这些受控宿主合同不代替新候选的 Windows OpenCode 实机结果。
- OpenCode v2 固定结构 fixture 经公开 wait/resume 命令覆盖会话归属、failed idle 错误、成功恢复和未终止重试；保留旧版回归。
- 首次全量 273 项中仅原有普通发送耗时断言失败（3.814 秒超过 3.5 秒）。基线与修复分支交替各测 4 次，同为 13 个 tmux 调用；基线 2.706–3.279 秒，修复 2.690–3.271 秒。没有测得新增延迟；原路径及 3.5 秒断言均保留。最终在相同工作树执行 `WFC_HERDR_TEST_BINARY=/tmp/wfc-herdr-0.9.5 python3 -m unittest discover -s tests -v`：274 项，全部通过、3 项仅限 Windows 的测试跳过，耗时 534.684 秒；该耗时断言通过。

Windows 候选下载、校验与复测步骤见 [候选复测说明](windows-opencode-candidate.md)。优先记录无人工 Enter 的首派闭环和实际会话 resume；5 分钟延时单独验收。

最终双轴独立审查：Standards 与 Spec 均无未解决发现。PR 的原生 Windows CI 及候选实机结果以对应检查和后续记录为准。
