# Router 实现参考与采用方式 · 2026-11-13

产品方向见[Router foundation](router-foundation.md)。本页固定本次研究结论，区分可审实现、官方接口说明与未执行探针；所列机制均不代表已经导入 wfc。源码版本记录在[来源清单](research-source-pins-20261113.json)。

## 总结

| 项目 | 与 wfc 的关系 | 本轮最有价值的机制 | 采用方式建议 |
|---|---|---|---|
| **Orca** | 外部 Agent 编排直接重叠，完整产品还含工厂外壳 | 持久 delivery/显式 ACK、controller generation、幂等 mutation receipt | 最先借鉴消息与接管协议；独立 runtime 作为后续可选依赖 |
| **Agent Autoloop** | 后台 CLI 事件调度直接重叠 | claim/本轮执行凭据、回执去重、unknown、已开工与未开工恢复区分 | 吸收轻量状态机与故障验证方法；事件中台后置 |
| **LobeHub** | 外部 coding runtime 层直接重叠；内部模型循环另属一层 | 原生 thread/turn、协议 interrupt/resume、accepted ACK、cursor/gap | 可选宿主协议适配参考；完整平台为外壳候选 |
| **Herdr** | 持续终端运行层重叠，适合委托底层设施 | 跨平台 PTY/IPC、输入、session restore、自报告与事件序号 | 首选可选 TUI 后端探针，Windows 与平台薄层一起验证 |
| **Octopus / octo** | Provider 派发与上层工作流部分重叠 | 多信号 stall、故障分类/锁出、checkpoint、取消预算 | 吸收机制；其工作流、备用模型和审查政策保持在上层 |
| **Paseo** | 原生协议 agent daemon 和远程控制层重叠 | turn ID、先监听后发送、串行生命周期、取消确认与恢复、公开 SDK | 可选 daemon backend；额外常驻服务与 SDK 成本需实测 |
| **Codeg** | ACP 会话与 delegation broker 重叠，完整产品是工作台 | setup/取消/完成竞态、父子执行关系、Windows launcher/IPC | 平台与生命周期参考；未来外壳或显式选用的执行服务 |
| **Tern.build** | 官方面向 Agent 工作台、daemon、任务与审查队列 | CLI status/read/send/wait 的外壳接口形状 | 官方声明闭源；本轮未读核心 router，非代码融合或 Windows 底座候选 |
| **Pachat.ai** | Agent 群聊与唤醒入口，非代码任务执行底座 | 消息 opKey、cursor/long-poll、群组会话归属、运行次数预算 | 未来可选群聊入口；仅公开客户端可读，服务端语义未源码验证 |

## 1. Orca：最直接的持久邮箱参考

最新源码 `role-mailbox-delivery.ts` 把消息批次、delivery ID、consumer generation 和 ACK 分开；`mailbox-consumer.ts` 校验当前消费者 generation，旧消费者被 fenced。`mutation-receipt-store.ts` 用 caller/request ID/method/payload hash 记录变更请求；重复请求可回读，内容不一致则拒绝。这比单个 delivered 布尔值更适合处理换总控、回报后崩溃和启动结果未知。

其中也有即时确认的 take 路径，不能泛称所有读取都等处理确认。wfc 应借鉴具体契约而非照搬命令名。Orca 同时有终端和 structured-session 通知路径；其当前编排仍标 Experimental。详见[Orca/Paseo 源码研究](orca-paseo-router-research-20261113.md)。[邮箱](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/src/main/runtime/orchestration/db/messages/role-mailbox-delivery.ts#L66)、[generation](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/src/main/runtime/orchestration/db/messages/mailbox-consumer.ts#L15)、[mutation receipt](https://github.com/stablyai/orca/blob/075d6b3b507b8b6ce0c3202e72a5bb00c4045500/src/main/runtime/orchestration/db/mutation-receipts/mutation-receipt-store.ts#L36)。

## 2. Agent Autoloop：轻量状态机与回执

`kernel/decide.ts` 的 decide 把状态视图、输入、时间与环境变为事实；`store.ts` 在事务中写日志和投影，可从日志重建。claimNext 让同 claim 重试得到原任务，finish 按本轮凭据和结果摘要去重；sweep 区分 worker 离线和运行结果 unknown。

`worker/agents.ts` 把 command/read/session/started/finished/error 明确化；worker/conclude 仅在本轮没 started 时尝试全新 session，已开始的失败保留恢复关系；错误回执只续写格式。Codex/Claude 使用 exec/print 结构化输出，与可 attach 的 TUI 不同。

建议借鉴轮次归属、unknown 和回执幂等，不先引入整个 Node 中台。租约心跳不是有效进展证明；默认 180 分钟轮次上限也不是精确卡死检测。相同回执只应用一次不等于 shell/外部操作恰好执行一次。[decide](https://github.com/KKKKhazix/agent-autoloop/blob/c9ec4a187cab49627947e7e33db265ec143db3b8/kernel/decide.ts#L305)、[finish](https://github.com/KKKKhazix/agent-autoloop/blob/c9ec4a187cab49627947e7e33db265ec143db3b8/kernel/decide.ts#L455)、[store](https://github.com/KKKKhazix/agent-autoloop/blob/c9ec4a187cab49627947e7e33db265ec143db3b8/kernel/store.ts#L32)、[worker](https://github.com/KKKKhazix/agent-autoloop/blob/c9ec4a187cab49627947e7e33db265ec143db3b8/worker/worker.ts#L372)。

它还有独立重放不变量与真实 SIGKILL 测试定义，可参考为 wfc 的消息崩溃窗口设计小型故障测试；本轮没运行这些测试。[invariants](https://github.com/KKKKhazix/agent-autoloop/blob/c9ec4a187cab49627947e7e33db265ec143db3b8/sim/invariants.ts)、[crash tests](https://github.com/KKKKhazix/agent-autoloop/blob/c9ec4a187cab49627947e7e33db265ec143db3b8/sim/crash.test.ts)。

## 3. LobeHub：结构化宿主协议与可靠事件

外部 `heterogeneous-agents` 与模型内部 `agent-runtime` 要分开。CodexThreadSession 持有 thread/turn，支持 start/resume/interrupt、防同 thread 并行 turn；BatchIngester 区分 accepted ACK、按序重试、有界队列；GatewayMuxClient 使用 operation 的 seq/cursor 和 gap 通知。

这些适合 wfc 的可选协议执行路径，并为核心消息层提供参考；不需要同步引入完整 Web、DB、Redis 与 UI。其 publish 去重部分是进程内状态，源码明确冷副本重试可能重放部分子 Agent 文本/trace，不能称全局 exactly-once。[thread](https://github.com/lobehub/lobehub/blob/4c1b5434d999b3c6cfdbf83f246f9bd65b52aa78/packages/heterogeneous-agents/src/codex/CodexThreadSession.ts#L123)、[BatchIngester](https://github.com/lobehub/lobehub/blob/4c1b5434d999b3c6cfdbf83f246f9bd65b52aa78/apps/cli/src/utils/BatchIngester.ts#L58)、[cursor](https://github.com/lobehub/lobehub/blob/4c1b5434d999b3c6cfdbf83f246f9bd65b52aa78/packages/agent-gateway-client/src/mux/GatewayMuxClient.ts#L94)、[去重限制](https://github.com/lobehub/lobehub/blob/4c1b5434d999b3c6cfdbf83f246f9bd65b52aa78/apps/server/src/services/heterogeneousAgent/HeterogeneousPersistenceHandler/index.ts#L189)。

## 4. Herdr：Windows TUI 后端候选

已读 create/prompt/read/close、resume planner、状态来源与事件实现。它有 source/seq 自报告、事件历史缺口与快照恢复，不只是看屏幕；但官方 Claude/Codex 来源状态仍受原生屏幕判定规则约束，512 条内存事件也不能替代持久消息确认。Windows 的 ConPTY、Named Pipe、process creation identity 已有实现。

可由外部 CLI 提供终端能力，wfc 持有任务与消息。它不是已经接入的后端；env/cwd/argv、ownership、旧 pane 复用和输入未知结果都需闭环探针。详见[Windows 评估](windows-router-assessment-20261113.md)。[输入](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/app/api/agents.rs#L140)、[Windows 平台](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L1018)。

## 5. Octopus：只吸收底层机械能力

本轮已拉取并读 dispatch、session、heartbeat、provider-failure/lockout、spawn。它有工作流 checkpoint 与部分原生 agent resume，并非没有持久恢复。

`_octo_capture_provider_with_stall_watchdog` 联合 stdout/stderr size/mtime 与工作树改动元数据，较整屏变化更适合发现假活跃；capture 默认窗口是 0，已读 spawn 路径只对部分实施角色启用。PID heartbeat、输出增长与任务有效进展不是同一个事实。错误分类/锁出有价值，但换模型、审查席位和阶段门禁属于上层；Unix 进程组 TERM/KILL 不能原样成为跨平台取消。

该固定版官方原生平台是 Linux/macOS，Windows 用 WSL/SSH，故不作为本轮原生 Windows 底座。[watchdog](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/scripts/lib/heartbeat.sh#L629)、[启用调用](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/scripts/lib/spawn.sh#L1702)、[checkpoint](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/scripts/lib/session.sh#L417)、[平台范围](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/README.md#L126)。

## 6. Paseo 与 Codeg：可选执行服务，避免带入整个工厂

Paseo 的 provider runner 先订阅再启动 turn，按 turn ID 过滤；AgentManager 生命周期串行化，中断请求与 settled 分开，恢复从持久状态驱动。公开 client SDK 比摘 server 私有文件更适合接入；取消结算不必然等于全部 OS 子进程已经消失。详见[专项报告](orca-paseo-router-research-20261113.md)。[runner](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/packages/server/src/server/agent/providers/provider-runner.ts#L15)、[SDK](https://github.com/getpaseo/paseo/blob/d13438ba50ed74c0d9057370b5f5c781aee0f687/README.md#L135)。

Codeg 的 ACP manager 与 delegation broker 区分 Running ACK、long-poll、终态和中断恢复，并处理 setup 阶段提前到达的取消/完成。其默认 delegation 是一次子任务，resume 不是 wfc 的下一轮 continue；broker 完成正文内存有界缓存也不能取代落盘报告。Windows 的 launcher shim、UNC cwd、进程树与 Named Pipe 代码值得作为平台测试参考。详见[专项报告](codeg-router-research-20261113.md)。[broker](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/delegation/broker.rs#L37)、[process](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/process.rs#L1)。

Orca/Paseo/Codeg 均有原生 Windows 路径和构建/测试配置；本轮未在 Windows 运行，未把配置存在当作所有宿主验收通过。引入它们整个运行时会增加 daemon、SDK、DB、UI/平台依赖与第二套调度所有者，优先保持为显式的外部 adapter。

## 7. Tern.build：文档可查，核心源码不可审

用户确认的是 tern.build 工作台。官方说明闭源、发布二进制与校验和；桌面为 macOS，CLI/TUI 为 macOS/Linux/FreeBSD，未提供本轮原生 Windows 路线。CLI 文档有 status/read/send/wait、任务和编排入口，可作为外壳接口形状参考。没有核心源码，不能比较其 ACK、取消或恢复内部正确性；公开安装/Release 仓库不等于 router 开源。[官网 FAQ](https://tern.build/)、[CLI](https://tern.build/docs/cli)、[起步文档](https://tern.build/docs)。

本次只读公开网页；直接读取安装脚本返回 403，未下载或运行其二进制，没有启用 daemon。它不作为代码融合来源或 Windows 解法。

## 8. Pachat.ai：群聊入口与公开 listener 客户端

已读取官方 install.md 和公开单文件 pachat.mjs，保存日期与 SHA-256 在来源清单。未找到可核实的服务端公开仓库或客户端重分发许可声明，故不复制其代码，也不声称审过服务端 router。

客户端 runAgentFor 按 group 保存原生 session、notes 内容摘要；listenLoop 串行处理待办、限制每日次数与同一群消息的尝试次数。失败后的已知 session 被清除，下一次重建：这不能直接用于有未提交工作/外部副作用的 wfc 接替。spawnRun 超时先 TERM 后 KILL，但未在本轮证明整棵子进程树已停止。

官方协议有 opKey 防重复发消息、cursor/long-poll；read 会标已读，不能等同调用方业务已处理。listener 的 login service 实现仅 macOS launchd / Linux systemd，官方让 Windows 用定时 check-in；不构成 Windows worker 后端。它适合未来通知/群聊入口，不负责开发工厂的候选与交付语义。[公开客户端](https://pachat.ai/pachat.mjs)、[协议与 listener 说明](https://pachat.ai/install.md)。本轮只读，没有配对、登录、发信、注册服务或加入群组。

## 许可证与复用范围

Orca、Agent Autoloop、Octopus 根为 MIT；Herdr、Paseo、Codeg 根为 Apache-2.0；LobeHub 根为 Community License，agent-runtime 的单独 MIT 声明不能扩大到 heterogeneous runtime。Tern 核心闭源；Pachat 可读脚本没有据本次观察确认再分发许可。先按协议独立适配/参考方法，若复制源码或分发依赖，再按真实采用文件维护版权、NOTICE/许可证和来源版本；本轮没有导入第三方实现。
