# Orca、Paseo 与 workflow-core 的调度实现比较

调查日期：2026-11-13。本文是只读源码分析，不是集成结果，也没有运行两个项目或重新做 Windows 真机验证。定位：wfc 是小而完整的 router 基础；开发工厂流程与操作外壳由上层负责，现有上层为 super-coding。

固定源码：

- Orca `stablyai/orca`：`075d6b3b507b8b6ce0c3202e72a5bb00c4045500`。
- Paseo `getpaseo/paseo`：`d13438ba50ed74c0d9057370b5f5c781aee0f687`。

## 结论与采用层次

| 项目 | 与 wfc 的重叠 | 适合的关系 |
|---|---|---|
| Orca | 有明确 Run/Task/Dispatch、worker 启动、问题/完成消息、邮箱消费、唤醒和恢复，属于直接重叠；完整产品还包含 IDE/终端/远端/工厂能力 | 最值得参考持久邮箱、consumer fencing、幂等 mutation receipt；也可探索 Orca 作为可选外部执行 backend，保留 wfc 的稳定协议和交付核对 |
| Paseo | daemon 管原生 agent 协议、持久会话、发信、等待、取消、跨设备连接，属于运行与通信层重叠；完整产品是 agentic 开发环境 | 更适合作可选 daemon backend / SDK adapter；wfc 负责任务轮次和交付语义，上层流程不迁入 daemon |

两者都比“终端美化工具”范围大。也不宜因为有编排能力，就让 wfc 吸收其全部任务看板、DAG、代码审查、编辑器和 UI。[Orca 编排契约](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/docs/site/content/docs/cli/orchestration.mdx)、[Paseo 产品及 SDK](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/README.md#L42-L50)

## Orca：最值得读的核心

### 邮箱不是一个 delivered 布尔值

`role-mailbox-delivery.ts` 把一组 message IDs 绑定到独立 delivery ID 和 consumer generation。未确认批次可以 replay；显式 acknowledge 有事务校验并对重复 ACK 返回 duplicate。`mailbox-consumer.ts` 在事务内检查当前 run/dispatch 的 generation，旧总控读邮箱会收到 `consumer_fenced`，避免换总控后两个消费者继续派活。[投递与确认实现](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/src/main/runtime/orchestration/db/messages/role-mailbox-delivery.ts#L66-L208)、[消费者隔离](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/src/main/runtime/orchestration/db/messages/mailbox-consumer.ts#L15-L47)

不要把该实现描述为“所有读取都会等显式 ACK”：同文件也有 `takeMailboxDelivery` 路径会立即标记 acknowledged；具体调用须选对语义。可以借鉴其 delivery 身份和 fencing，而不是照搬命令名。

`mutation-receipt-store.ts` 用 caller fingerprint + request ID + method + payload hash 持久化变更请求。相同 request ID、不同内容会拒绝；已有 pending/completed 返回原状态，从而使“启动请求超时后到底有没有执行”有可查询凭据。[mutation receipt](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/src/main/runtime/orchestration/db/mutation-receipts/mutation-receipt-store.ts#L36-L121)

**对 wfc 的建议：** SQLite 层引入 controller generation、delivery batch ID、显式处理确认、可查询 mutation request ID，分别解决接管重复消费、已送达但未处理、启动不确定性。无须引入整个 Orca 工厂。

### 持续 TUI 与结构化会话都有路径

最新 mailbox notification coordinator 同时有 terminal pointer delivery 和 structured-session pointer delivery，后者明确没有 PTY 可以打字，不能再把 Orca 概括为“只有终端键盘注入”。它根据邮箱归属、等待者和保留消息种类唤醒消费者。[通知协调器](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/src/main/runtime/orchestration/mailbox/notifications/mailbox-notification-coordinator.ts#L15-L111)

编排文档定义 Dispatch 为本次 task attempt 的完成权威，`worker_done` 要带 taskId/dispatchId；`worker-start --retry-of` 是新尝试关系，模型/工作树/远端位置有显式限制。任务/DAG/决策门属于 Orca 上层，wfc 可只参考 attempt 身份和控制协议。当前编排仍标为 Experimental，CLI 需要正在运行的 Orca runtime。[契约及恢复命令](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/docs/site/content/docs/cli/orchestration.mdx#L7-L110)

最新文档说失败/不确定 CLI 结果非零退出；这与本仓库 [2026-11-05 Orca 探针](probes-20261105-orca.md) 当时“失败 exit 0，必须查 ok”的事实不同。旧探针也曾遇到 OpenCode 模型错误未唤醒。本次没有重跑，不能把旧缺陷宣称仍存在，也不能仅因新文件存在就宣称已通过真机回归。

### Windows 与独立使用限制

当前 README 有 Windows 安装包，仓库有 Windows native/WSL 的独立 CI 和原生终端路径；Settings 文档写明 Windows 默认 PowerShell/CMD，SSH 文档为 Windows relay 使用 node-pty prebuild。这是原生实现路径的证据，不是“只能通过 WSL”。但每个 agent 可执行文件、模型参数能力仍受执行主机支持约束；编排契约明确 OpenCode effort 不支持，模型覆盖也有条件。[平台与安装](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/README.md#L262-L265)、[SSH 实现要求](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/docs/site/content/docs/ssh.mdx#L95-L99)、[Windows CI](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/.github/workflows/windows-native-preparation-benchmark.yml)

Orca 主要是 Electron/TypeScript 应用，多处核心模块直接依赖其 runtime、DB 和 native chat registry，不是一个可直接 pip import 的 router 库。若依赖它，优先 CLI/RPC adapter 并明确版本范围；若吸收源码，MIT 允许但须保留相应版权/许可及第三方要求。[根许可证](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/LICENSE)

## Paseo：协议运行层比 TUI 看门狗更接近底座

### 统一的 turn/session 接口

`provider-runner.ts` 先 subscribe，再启动 turn，缓冲早到事件并按 turn ID 筛选，等待 `turn_completed`、`turn_failed` 或 `turn_canceled`；避免“发起后才监听”漏掉极速完成。provider 使用具体协议：Codex app-server transport、Claude 原生 agent 查询、OpenCode provider 等。这里是结构化 agent 会话，不是替每个 worker attach 原生交互 TUI；产品另有 terminals，两条路径不能混为一谈。[turn runner](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/packages/server/src/server/agent/providers/provider-runner.ts#L15-L108)、[Codex transport](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/packages/server/src/server/agent/providers/codex/app-server-transport.ts)

`AgentManager` 给事件附本轮 turn ID，前台生命周期变更串行化。`cancelAgentRun` 先请求 `session.interrupt()`，等待确认与本轮 settled；不确认则返回 refused，reload/replace/rewind 不继续。已确认但迟迟不结束会按超时结算 turn_canceled。这里的 force-cancel 包含 daemon 侧状态结算，不能等同于一定已杀净所有 OS 子进程。[取消流程](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/packages/server/src/server/agent/agent-manager.ts#L3823-L3928)

`restart-recovery` 在 daemon startup 修复遗留 running/initializing 状态，持久保存 resumeOnStartup，按配置筛选未归档的可恢复 agent，每个 agent 独立恢复并可取消等待，避免慢 provider 阻塞全部恢复。这不是跨宿主无损搬运上下文，仍须原 provider 的 session/resume 支持。[恢复实现](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/packages/server/src/server/agent/restart-recovery/index.ts#L1-L87)

**对 wfc 的建议：** 明确 turn ID 与交付任务身份，先监听后发送；中断有 accepted/refused/settled 结果，切换宿主前确认旧执行权释放；恢复由存储事实驱动，不只复原屏幕。Paseo 的 provider 原生事件可解决“计时器仍刷新”的部分盲区，但不能据此保证检测所有挂起。

### 外部依赖与 Windows

Paseo 有单独 daemon 和公开 TypeScript client，README 给出 `createPaseoClient`、`agents.create`、`waitForFinish`，CLI 有 run/send/attach/remote host；这比将其 server 内部文件摘进 Python 更适合作 adapter。代价是常驻 daemon、Node/TypeScript SDK、鉴权连接和 provider 版本兼容，不再是零额外服务的单文件形态。[SDK 和 CLI](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/README.md#L135-L176)

Windows 有 desktop release 和 server 单测/集成 CI，provider launch parity 测试覆盖 claude/codex/opencode/generic-acp/pi 的 Windows 启动参数；实现经统一 spawn/executable resolution 处理。因此可列原生 Windows 候选，但本次只见源码与测试定义，未读取最新 CI 成功结果，也未实际在 Windows 跑 agent。[Windows CI](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/.github/workflows/ci.yml#L238-L278)、[Windows provider launch 测试](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/packages/server/src/server/agent/providers/provider-windows-launch.test.ts#L20-L30)、[发布工作流](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/.github/workflows/desktop-release.yml#L342-L348)

Paseo 根许可证为 Apache-2.0，第三方组件保留各自许可；若复制代码须履行相应通知/许可要求，使用 SDK/协议依赖也应固定版本。[LICENSE](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/LICENSE)

## 最小下一步

先在 wfc 自有 SQLite 协议补 delivery/ack、controller generation、attempt/turn identity 与可查询恢复状态。然后只做一个可选 backend 探针：Paseo 验证 create→send→question→interrupt→resume→完成与断线恢复；Orca 验证 mailbox batch replay/ack、consumer 替换和不确定启动 receipt。两者都须检验原生登录、工作树、报告候选身份及取消后的旧 writer；不因有可用 SDK 就迁移上层开发工厂流程。
