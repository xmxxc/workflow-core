# Native Windows router assessment · 2026-11-13

状态：本次源码评估与建议，尚未实施或做原生 Windows 验收。本文把 workflow-core 定位为可供不同上层调用的小型通用 router：负责派发、输入、结果、消息、异常和恢复；业务流程、角色政策和界面由调用方持有。

## 结论

**最小可行路线是保留 Python router 与 SQLite 状态，增加一个 Herdr 终端后端及薄平台层。** Herdr 承担 Windows ConPTY、终端持久运行、输入和窗口管理；wfc 继续拥有 dispatch、任务与报告、消息确认、恢复关联及可观察结果。tmux 后端继续服务现有 Unix 用户。

这需要改动当前实现中的 Unix 假设，不能只替换 `tmux` 命令。无需先改用 Rust/Go，也不应把 Herdr 整个运行时或 Octopus 工作流复制进 router。是否值得正式依赖 Herdr，应由下文最小验证矩阵决定。

## 观察基线与证据身份

- workflow-core：`22a3099be424ea94bdef2d575dce6344c00398ca`，版本 `wfc 0.4.5`。本仓引用均为相对源码路径。
- Herdr：`ee5bfb5041bb3e4a49d591c72308aae3c81ecdde`，固定提交源码阅读。
- Octopus：`a4191b7f60622c2543d8c06f2feda72ded8448a1`，固定提交源码阅读。
- 实际执行：读取上述源码、构建配置、已有测试、许可证及 Python 官方平台契约；未运行外部仓库、安装后端、改写运行时、提交或推送。
- 所有“应/建议/最小方案”均为本轮设计建议；上游已有代码不能当作 wfc 已具备该能力，源码中的测试不能当作本轮已执行。

## 当前 Windows 障碍

| 领域 | 当前实现 | 原生 Windows 影响 | 最小处置 |
|---|---|---|---|
| 导入 | [router.py](../router.py) 顶层 `import fcntl` | 在标准 Windows Python 上无法导入；即使只查状态也进不了命令 | 平台选择后再导入锁实现，保持同一窄接口 |
| 终端生命周期 | `tmux`、`session_alive`、`launch`、`capture`、`type_into`、`session_owner`、`reclaim_one` | 直接依赖 tmux 会话、环境读取、capture-pane 与 send-keys | 抽取终端后端操作；Herdr 仅覆盖运行/观察/输入/关闭 |
| 输入互斥 | `input_lock` 与 Claude 配置写入使用 `fcntl.flock` | 标准库该实现不可用；两个派发输入可能交错 | Unix 保留 flock；Windows 使用显式的文件区间锁实现，统一超时/释放语义，保留“输入锁在数据库事务前”的顺序 |
| waiter 身份 | `process_start` 读 procfs，fallback `process_lstart` 运行 Unix `ps`；`process_alive` 用 `os.kill(pid, 0)` | 没有 Unix ps；仅 PID 不能防复用；Windows kill 不是可直接套用的 Unix 探活契约 | Windows 使用只读 process handle、退出状态及 creation time；不向探测目标发送信号 |
| 后台 relay | `cmd_relay --detach` 使用 `start_new_session=True` | 参数提供的是 POSIX setsid 语义，不能证明关闭 Windows 终端后 relay 继续运行 | 平台专用 detach，明确 console/job 生命周期；或把 router 的 relay 放在受后端管理的专用运行单元，不能混同 worker |
| 回调命令 | `worker_brief` 用 `shlex.quote` 拼 Python、程序和报告路径 | POSIX shell quoting 不适用于 PowerShell/cmd；空格、单引号、盘符可能出错 | 回调携带结构化 argv/路径数据；若仍展示命令，按明确 shell 渲染，避免通用 shell 字符串 |
| 文件/路径 | 默认状态位置、OpenCode 数据位置按 Unix习惯；若干 JSON/text I/O 未显式 UTF-8；路径比较与信任规则用 `Path.resolve` | 并非所有路径必然失败，但系统数据根、大小写、盘符、junction、长路径与默认编码需逐项验证 | 保留显式环境覆盖，增加平台默认值与 UTF-8；所有权用固定身份/nonce，不能仅依赖路径文本相等 |
| 配置保护 | `_trust_locked` 的 fchmod、Unix mode、替换写入与信任继承 | Unix mode 不能等价表达 Windows ACL；配置被宿主占用时替换语义也不同 | Windows 路径先安全降级为不改宿主信任配置，保留人工确认；需要自动配置时另做平台实现 |
| CLI 分发 | [build.py](../build.py) 生成带 Unix shebang 的无扩展名 zipapp；[release workflow](../.github/workflows/release.yml) 在 Ubuntu 打包 | Python zipapp 可复用，但双击/命令查找不能靠 Unix executable bit/shebang | 同源 `.pyz` 加很薄的 Windows 启动入口；显式 Python launcher/解释器探测 |
| CI 与测试 | [CI](../.github/workflows/ci.yml) 仅 Ubuntu/macOS；[test_router.py](../tests/test_router.py) 也导入 fcntl，fake CLI 依赖 shebang/chmod，部分测试用 symlink | 不能只把 matrix 加 windows 就宣称支持 | 抽出跨平台契约测试，fake 后端独立于 shell；保留 Unix 特定测试并新增真实 Windows 后端验证 |

Python 平台依据：[fcntl](https://docs.python.org/3/library/fcntl.html)、[msvcrt.locking](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)、[os.kill](https://docs.python.org/3/library/os.html#os.kill)、[subprocess start_new_session](https://docs.python.org/3/library/subprocess.html#subprocess.Popen)。`msvcrt.LK_LOCK` 的内置重试并非无限等待，不能在封装里默默改掉现有锁语义。Windows `os.kill` 的控制台事件/TerminateProcess 行为不应被当作 Unix `kill(pid, 0)` 的探活实现。

## Herdr 已实现什么

### 原生终端与 IPC

Windows 分支通过 `portable_pty::native_pty_system()` 创建 PTY 并 spawn command；平台层已有 Named Pipe 连接、连接超时、权限描述符、可取消/轮询读取等实现。wfc 可以调用 Herdr 的 CLI，让它负责平台 IPC，避免在 Python 中再实现一套 Named Pipe client。JSON socket 协议是备用集成方式，不是首轮必须新增的第二条传输。

证据：[Windows PTY](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pty/backend.rs#L7)、[Named Pipe listener](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L28)、[connect timeout](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L152)。

### 运行与输入接口

`workspace.create` 与 `pane.split` 提供 cwd/env；`AgentStartParams` 接受 kind、name、pane_id、args；`LayoutPane` 还能携带 command argv。`agent.prompt` 校验目标前台身份和 blocked 状态，队列化 text/Enter；Windows Codex 有粘贴边界处理。已有 pane read/send keys/close 等操作。

适配时可用专有 workspace/pane 设置 router 环境，再按 argv 启动受支持宿主。须实际验证 resume 参数、额外环境继承和命令路径解析；`agent.start` 没有独立的 env 字段，不能臆造该能力。使用 layout command argv 还是 agent.start 是首轮探针要固定的一个选择，不应先实现两套。

证据：[workspace 参数](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/api/schema/workspaces.rs#L9)、[pane 参数](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/api/schema/panes.rs#L23)、[layout argv](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/api/schema/panes.rs#L215)、[agent start 参数](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/api/schema/agents.rs#L184)、[prompt 校验与提交](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/app/api/agents.rs#L140)。

### 进程身份、恢复与状态

Windows `ProcessIdentity` 使用 OpenProcess/GetExitCodeProcess/GetProcessTimes；`process_start_token` 以 creation time 防 PID 复用。恢复 planner 按 source/agent/session ref 生成宿主 argv。可借鉴其实现模式，无须把 Rust 平台代码作为 Python router 依赖。

Herdr 也支持来源/seq 的状态报告、带序号事件与快照对账。但官方 Claude/Codex source 的 hook 状态不会覆盖原生屏幕判定；内存事件只保留 512 条，历史缺口返回 `events_lost`。因此适配后仍需 wfc 自己保证任务完成、持久消息确认和有效活动检测。

证据：[ProcessIdentity](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L1018)、[creation token](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L1940)、[resume planner](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/agent_resume.rs#L199)、[state source handling](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/app/actions.rs#L1490)、[event history](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/api/event_hub.rs#L18)。

## 最小后端接口建议

以下为实现内部接口，不要求把每项都扩成新的 CLI 子命令：

| 操作 | 输入/输出核心契约 |
|---|---|
| create | argv、cwd、env、label → 固定 runtime handle；结果区分 started / definitely-not-started / uncertain |
| inspect | handle → alive、host/session identity、readiness、blocked、状态版本；读不到与已退出分开 |
| read | handle → 屏幕/输出快照及观察游标；数据缺口显式返回 |
| submit | handle + text + submission ID → accepted / rejected / uncertain；accepted 只表示终端提交，不表示任务完成 |
| interrupt | handle + 明确操作意图 → 恢复/中断结果；与 close 分开，不能把 Ctrl-C 当通用“催办” |
| close | handle + 所有权 token → 只关闭该 router 拥有的运行单元；不关闭共享 workspace/其他 pane |

handle 至少需要持久记录 backend、backend endpoint/server identity、workspace/pane/terminal identity 与 ownership token。标签只是展示名。旧数据库没有 backend 字段的 dispatch 按原 tmux 解释，不在恢复时根据系统或 PATH 自动迁移已有任务。

终端后端、宿主适配、router 核心各司其职：后端处理终端；宿主适配构造 argv、识别宿主 session/error；核心维护轮次、消息和恢复。平台层仅封装文件锁、进程身份、detach 与命令/路径处理。wfc 不需要拥有具体上层角色或审查政策。

## 最小交付顺序

1. **先形成稳定后端契约**：把现有 tmux 行为移入适配，原有回归不变；SQLite/命令语义维持兼容，避免同时重写状态机。
2. **平台基础可导入**：隔离 fcntl，替换 Windows 探活，处理 UTF-8、回调、打包。先让 Windows 的 help/version/status/状态库操作可用。
3. **Herdr 单宿主闭环探针**：create → task → ask/reply → done → continue → exit/resume → reclaim；先选一个宿主，确认 env/cwd/argv 与所有权后扩至其他宿主。
4. **恢复与消息语义**：router 重启、后端重启、投递超时、晚到回报与重复输入；不能用 Herdr 屏幕 idle 代替 done。
5. **Windows 发布条件**：独立 Windows CI、真实原生终端演练和已知限制；只读源码分析不计入通过。

## 验证矩阵（建议，全部待执行）

| 场景 | 平台/后端 | 可接受结果 |
|---|---|---|
| import/help/version/只读 status | Windows Python 3.9/3.12；Unix 原 tmux | Windows 不导入 Unix 专属模块；只读不创建状态/锁；Unix 结果不变 |
| create/env/cwd/argv | Windows Herdr，PowerShell/cmd 启动；空格、Unicode、盘符路径 | 参数不被 shell 二次解释；worker 收到本轮独立状态/报告路径 |
| 输入并发与轮次切换 | Windows Herdr 与 Unix tmux | reply/nudge/continue/done 并发不交错；旧 round 不完成新 round |
| 首次权限确认与 blocked | 各宿主 | 暂停并报告，恢复后继续；不盲发 Enter 或绕过用户选择 |
| 慢/丢失提交 | 后端 API timeout、粘贴队列延迟 | uncertain 可见，不盲重发；最终仅一次可识别任务输入 |
| 关闭启动终端/重启 router | Windows Herdr | worker 与所需 relay 生命周期符合声明；同一 dispatcher 不重复接管 |
| 宿主正常退出/异常退出/恢复 | 每个声称支持的宿主 | 找回正确宿主 session；不存在/不可写与运行中区分 |
| PID 复用/权限拒绝 | Windows process adapter | 只读查询不发信号；creation token 不同不能认作旧 waiter |
| 文件锁持有者崩溃/竞争 | Windows file lock | 可释放/恢复，有界等待；写入顺序和原状态机一致 |
| 事件历史缺口/Herdr server重启 | Herdr | 重新 snapshot 并对账；不会把旧handle误绑定新pane |
| 单票回收/共享窗口 | Herdr workspace/pane | 只处理有匹配 ownership token 的运行单元；其他任务保留 |
| Windows CLI shim/路径与JSON | `.exe`/`.cmd`/`.ps1` 入口及 `.pyz` | 可确定查找方式；不依赖Unix shebang；UTF-8与换行一致 |
| 真实有效活动与假活跃 | 各宿主 | 屏幕计时器不能无限掩盖挂起；合法静默/等待不被自动终止 |
| 原生无WSL验证 | Windows 主机 | 并未借 WSL/SSH/Linux 来满足宣称的原生闭环 |

## Octopus 可复用点与边界

Octopus 已有多信号 stall 采样（stdout/stderr metadata + worktree fingerprint）、带预算的取消、错误分类、provider 锁出和原子阶段 checkpoint。可吸收这些模式到通用 router，但其 Bash 进程组 timeout 实现不能直接变成 Windows取消模块；默认备用模型链也不应进入 router 核心。

该固定提交的 README 明确原生支持 Linux/macOS，Windows 使用 WSL/SSH，且原生 Git Bash/MSYS2/Cygwin 不受支持。它不是本次原生 Windows 底座候选。其阶段、审查、工作流逻辑属于可选上层，不能作为 router 完整性前提。

证据：[Octopus 平台范围](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/README.md#L126)、[stall watchdog](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/scripts/lib/heartbeat.sh#L629)、[启用调用范围](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/scripts/lib/spawn.sh#L1702)、[checkpoint](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/scripts/lib/session.sh#L488)。

## 依赖与许可证

Herdr 根许可证为 [Apache-2.0](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/LICENSE)，Octopus 为 [MIT](https://github.com/nyldn/claude-octopus/blob/a4191b7f60622c2543d8c06f2feda72ded8448a1/LICENSE)。首轮推荐调用用户安装的 Herdr CLI，不复制 Rust/终端代码。若以后分发 Herdr 二进制或复制源文件，需按实际纳入的文件、依赖和许可证维护来源与声明；本次未做全依赖许可证审计。

Windows 路线与小型 router 不冲突：将 PTY/终端运行委托给已支持平台的程序，wfc 保留少量适配和确定性的调度协议。代价是多一个运行时依赖；是否接受应以完整闭环和故障恢复演练结果衡量，而非只看成功启动一个 agent。
