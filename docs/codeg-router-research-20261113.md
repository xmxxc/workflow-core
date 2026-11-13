# Codeg 调度源码研究 · 2026-11-13

本页为固定源码分析，服务于 workflow-core 的“小而完整的 router”定位。未安装、启动或测试 Codeg，未完成任何后端接入；建议与当前已实现能力分开记录。

## 来源与产品关系

- 仓库：[spacering-net/codeg](https://github.com/spacering-net/codeg)，本次浅克隆固定 HEAD `139ad96e5d54293a5833ccf2df825354ac2064ee`。
- 源码 manifest 显示开发版本 0.44.0；这不等于已发布 Release 或已安装版本。[Cargo.toml](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/Cargo.toml#L1)
- 根许可证 Apache-2.0；未逐项审完全部依赖的再分发条件，本次无上游代码导入。[LICENSE](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/LICENSE)

Codeg 整体是多 Agent 工作区，包含桌面、独立服务器、CLI、MCP companion 与客户端。其内部 ACP/外部 Agent 管理与协作 broker 和 wfc 同类；UI、代码编辑、仓库面板、自动化等属于工厂外壳与业务流程。它不是一份能直接导入 Python zipapp 的小型独立 router 库。[组件特征与独立二进制](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/Cargo.toml#L22)

## 核心实现

| 入口 | 已读事实 | 对 wfc 的启发 |
|---|---|---|
| `acp/registry.rs` | AgentDistribution 区分 npx、binary、uvx 与用户安装的 system；平台 entry/sibling 文件、能力信息单独记录 | Agent 能力与启动配置应从传输和流程策略中分离；不要要求每个宿主都支持同一组功能 |
| `acp/agent_session.rs` | 对 session/new、load、resume 后的更新安装 session handler；提前到达的带 sessionId 消息可交给注册后的 handler | 会话身份应明确，不能让“响应前到达的事件”变成失联消息 |
| `acp/manager.rs` | ConnectionManager 统一 spawn、send_prompt、cancel，绑定 conversation/connection 与宿主 external session；同一对话可复用已活跃连接 | 核心区分逻辑任务、运行连接、宿主会话；恢复不能仅靠 PID 或窗口名 |
| `acp/delegation/broker.rs` | delegate 返回 task_id 的 Running ACK；status 支持 long-poll；终态在锁内从 running 移到 completed 并通知等待者 | 启动确认、等待与真正完成应是不同结果；核心状态查询不消耗消息 |
| `acp/delegation/spawner.rs` | ConnectionSpawner 把 broker 决策与具体 spawn/send/cancel/disconnect 操作分离，且 resume 返回 fresh/reused | 可借鉴少量内部执行接口，避免借一次抽象重写全部宿主 |
| `acp/event_stream.rs` / `internal_bus.rs` | 连接事件有序号、有限近期缓冲；超出保留区间时回快照；类型化内部事件总线与 Web JSON 广播分开 | 有持久状态、游标和快照契约，外壳才能重连；有限内存总线不能替代持久消息 ACK |

证据：[registry](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/registry.rs#L1)、[session handler](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/agent_session.rs#L1)、[manager spawn](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/manager.rs#L867)、[broker 生命周期](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/delegation/broker.rs#L1)、[spawner](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/delegation/spawner.rs#L89)、[event stream](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/event_stream.rs#L1)、[internal bus](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/internal_bus.rs#L1)。

### 恢复、取消和父子关系的具体边界

Broker 当前默认 delegation 是一次子任务会话；`resume_delegation` 只对中断/崩溃遗留任务，使用原 task_id、记录的宿主 session、assistant 快照和目录恢复，不是 wfc `continue` 式下一轮修复。恢复 setup 自身有 claim，区分 fresh/reused，避免失败清理误关复用的连接。

父会话被显式 cancel 或断开会级联取消 children；正常 end_turn 保留 children 后台运行。setup 阶段也记录早到的取消与终态，防止 spawn 完才注册任务漏掉完成或取消。对此 wfc 应明确“总控暂时结束一轮/断线”和“用户明确取消整个范围”的不同策略，不能照搬父断开即全取消。

完成正文保存在内存 completed cache（父级隔离、FIFO/字节上限），被淘汰后 DB fallback 保留任务状态，全文仍从子会话读取；不能把它描述成持久 broker 保存了所有结果，更不能据此替换 wfc 的落盘报告。证据：[broker 类型与缓存边界](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/delegation/broker.rs#L37)、[resume 实现](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/delegation/broker.rs#L4054)。

## 外部控制与可选桥接

Web handlers 已有 Agent 列举/诊断、connect、prompt、cancel、fork、permission/question 回复等入口，复用 ConnectionManager。MCP companion 通过内部 broker transport 请求 delegate/status/cancel/resume；该内部协议按单轮 token 鉴权，并与 companion 版本耦合，不是承诺跨版本稳定的第三方 SDK。[HTTP handlers](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/web/handlers/acp.rs#L79)、[cancel](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/web/handlers/acp.rs#L466)、[broker transport](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/delegation/transport.rs#L1)。

推论：若将来用户选 Codeg 做外壳，可评估外部 API 薄桥，让 Codeg 展示与操作 wfc scope，或由 wfc 显式选用 Codeg 的执行服务。必须先决定哪边持有 session、重试和取消所有权，不让 Codeg broker 与 wfc 同时管理同一任务。源码存在控制入口不等于已有 wfc 插件；需要另做认证、版本、session/round 映射与崩溃恢复验证。

## 原生 Windows：有实现，但不能整套搬入就自动支持

Codeg 不依赖 tmux 来启动 Agent：ACP 走子进程 stdio。它对 Windows 有实际平台代码：

- `process.rs` 统一 CREATE_NO_WINDOW、UTF-8 环境，解析 `.exe/.cmd/.bat` shim；避免 Rust CreateProcess 默认搜索不到 npm launcher。
- `acp/agent_process.rs` 管理整棵进程树、spawn/exit 回调、Windows batch launcher 与 UNC cwd 的 pushd 路径，避免丢工作目录或误用回收 PID。
- delegation 使用 Unix socket 或 Windows named pipe。服务重启前先探测，在 Windows 不替换仍活着的 named-pipe accept loop，避免旧实例尚在时重绑失败。

证据：[process wrapper](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/process.rs#L1)、[ACP process](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/agent_process.rs#L1)、[service recovery](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/delegation/service.rs#L86)、[named pipe bind](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/src-tauri/src/acp/delegation/listener.rs#L364)。

CI matrix 明列 Linux/macOS/Windows 与 desktop/server/cli；Windows desktop 仅 `cargo test --no-run` 编译，server/cli 分支配置执行测试。Release 配置提供 Windows server targets。本次只读配置，未查询具体 run 是否通过，也未运行 Windows 实验，不能把“写有 CI”当全部 Agent 实测通过。[CI matrix 与限制](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/.github/workflows/test.yml#L74)、[Windows server release](https://github.com/spacering-net/codeg/blob/139ad96e5d54293a5833ccf2df825354ac2064ee/.github/workflows/release.yml#L749)。

## 对 workflow-core 的建议

1. **优先吸收生命周期模型。** task/attempt/connection/native session 分开；开始 ACK、等待人类、完成、可恢复中断和进程退出分别表示。借鉴 broker 对 spawn/完成/取消竞态的处理，继续保留 wfc 落盘报告。
2. **借鉴平台测试用例。** Windows npm shim、Unicode/空格路径、UNC cwd、整棵进程树、进程身份、named pipe 重启；wfc 先解决自身 fcntl/ps/Unix 引用依赖，再讨论外部后端。
3. **将 Codeg 留作可选外壳/执行服务候选。** 其完整 runtime 依赖 Rust/Tokio/SeaORM、ACP 和大量工作区模块，不能作为“加一个函数就支持 Windows”的 Python 库。wfc 如果仍重视原生 TUI，Herdr 后端试点更贴近现有体验；Codeg 的结构化 ACP 路线适合另一种执行能力。
4. **不迁入工厂政策。** assistant 角色、任务挑选、审查门禁、自动合并与 UI 属于上层。wfc 只提供可确认、可恢复、可取消和可观察的机械能力，由 super-coding 等外壳调用。

## 验证范围

实际执行：公开仓库 shallow clone、读取固定 SHA、相关核心源码/manifest/许可证/CI 配置；对照 wfc 当前入口。没有安装依赖、构建、运行上游测试或真实 Agent，没有登录服务、修改用户设置或部署集成。文档为新研究证据，后续实施需独立验证。
