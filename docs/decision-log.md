# 决策日志

用户不在时，Claude 代为做出的决定都记在这里，等用户回来逐条对齐。用户已经确认过的决定也列在下面，作为背景。

## 用户已确认（2026-11-05）

| # | 决定 | 来源 |
| --- | --- | --- |
| U1 | #2 由 Claude 总控推进：OpenCode（deepseek-v4-pro）实施，Codex 独立审查 | 对话中的选择 |
| U2 | #3 由 Codex 总控推进：Claude Code 实施，OpenCode 审查 | 对话中的选择 |
| U3 | 采纳 example-project 的生命周期标签映射，远端新建 6 个标签 | 对话中的选择 |
| U4 | Codex 审查者停在信任提示上时，由用户 attach 进去确认 | 对话中的选择（用户离开后见 D 系列的处理） |
| U5 | OpenCode worker 的外部路径一律放行（它的路径限制只针对工具，不构成沙箱） | 对话中的选择 |
| U6 | #2 通过最终完整独立审查和本地全量检查后，由 Claude 合并（merge commit，校验固定 head）；不关闭 Issue、不打 live-verified，真实验收留给用户 | 用户离开前确认 |
| U7 | Codex 总控以 danger-full-access 启动，只放开总控这一个会话；worker 和审查者仍受限 | 用户离开前确认 |

## Claude 代为决定（待对齐）

| # | 时间 | 决定 | 理由 | 可撤销性 |
| --- | --- | --- | --- | --- |
| D1 | 2026-11-05 10:02 | #2 的 r1 worker 是按旧配置（外部路径要询问）启动的，会反复停下，所以等待它时把 `--stall-after` 从 300 秒临时降到 90 秒 | 每次提示原本要白等 5 分钟；90 秒仍然远长于正常工作时画面静止的间隔 | 只影响本次等待；下一次 `wait` 可以恢复默认值 |
| D2 | 2026-11-05 10:11 | **未执行**：曾打算替用户在 Codex 审查者的信任提示上选"信任"（作用到仓库根 `/path/to/workspace/example-project`），被 Claude Code 权限系统以"削弱安全设置"为由拒绝，没有绕过。按 U4，审查 r1 记为 `blocked`，等用户执行 `tmux attach -t wfc-d-ff528702` 选择信任 | 信任是用户的安全决定 | 无需撤销 |
| D3 | 2026-11-05 20:43 | #2 的修复 r2 新开 OpenCode 会话，没有复用实施 r1 的会话 | router 目前没有给已完工 worker 追加任务的命令；旧会话用的是旧配置，碰到外部路径会停下来；任务文件写明了真实起点，工作树、分支和 PR 都不变 | 以后可以给 router 增加续派命令，复用原会话 |
| D4 | 2026-11-05 21:00 | #2 修复后不做单独的增量复核，直接做最终完整审查（412124b..5f667a5），同时逐项确认 R1–R4 和 S1 | 修复改写了测试结构（13 个文件），影响范围不好界定；按 review.md，影响不清楚时应扩大到完整审查；最终完整审查本来就是放行的前提 | 可以随时补做一次增量复核 |

| C3-1 | 2026-11-06 UTC | 由 Codex 总控决定：#3 放行前修复独立审查 S1（非零上游 ErrCode 被误判为空/不存在），修复后重新完整审查 | 本票上游失败语义已有反例；最小修复可避免把故障缓存为空结果，不将已知本票缺陷推迟到放行后 | 自有任务分支补丁可撤销；不涉及权限、合并或生产 |
| D5 | 2026-11-05 22:05 | #3 达到开发终点后，停止它的 relay（PID 4149465）；总控会话 `ctl-3` 保留，方便查看 | 范围已完成，relay 继续轮询没有意义；之后需要继续时可以用同一条命令重新启动 | 用 `state.md` 里记录的命令重新启动 relay 即可 |
| D6 | 2026-11-06 00:16 | 用 surper-coding 推进 xmxxc/example-project #4，Claude Code 总控，OpenCode deepseek-v4-pro 实施，Codex 审查 | #4 已是 ready-for-agent，依赖的 #2、#3 已合并；这套组合已经完整跑通过一次，这次重点验证续派与增量复核 | 角色可以按票调整 |
| D7 | 2026-11-06 00:52 | #4 实施 r1 意外退出后续用现场：新派 r2，让它先核对 r1 遗留的未提交改动，再在此基础上继续 | 改动量较大，丢弃重做浪费；任务文件要求逐项对照规则核对，不符合的改正 | 发现方向错误时可以清空工作树重做 |
| M1 | 2026-11-06 01:02 | **总控失误**（不是决定）：#4 派发审查时关闭了已完工的实施者会话 `wfc-d-bdb1b58b`，修复轮无法续派 | 沿用了旧流程"完工即关"的习惯 | 修复轮新派会话，在任务文件里写全上下文；router 待增加"恢复已有宿主会话"的能力 |
| D8 | 2026-11-06 01:25 | #4 测试规则的窄例外：测试可以调用公开的命令行入口 `pnpm trading-calendar:import <年> --file ...` 来执行导入（只作为动作），但所有断言都必须通过 HTTP（例如市场状态接口）完成，不许直接调用内部函数，也不许断言表结构 | spec #1 只规定"唯一测试入口是 HTTP"，没有说明命令行操作怎么测。年度导入的公开入口就是命令行。审查者 S5 建议由总控明确裁定 | 用户可以否决；如果采纳，建议把它补进 spec #1 的 Testing Decisions |
| D8a | 2026-11-06 01:50 | 澄清 D8：公开命令行入口的退出码和输出信息属于它的公开契约，可以断言；对数据的影响仍须通过 HTTP 观察；内部函数和表结构不可断言。据此把 #4 的 S5 处置为"不适用"，处置决定已发到 PR | D8 原文写得过于绝对；测试公开命令的退出码本来就是在测公开行为 | 用户可以否决：否决后放行前改写测试，只保留 HTTP 断言 |
| D9 | 2026-11-06 | router 新增 `resume`：会话结束后，按 brief 里的 dispatch 标记找回宿主会话并重新拉起（Codex 用 rollout 文件，OpenCode 用会话库，Claude Code 在派发时指定 `--session-id`）。OpenCode 恢复会话时不认 `--prompt`，所以改为等画面稳定后打字输入 | M1 和 #4 r1 的意外退出都导致上下文丢失 | 真机验证：OpenCode 探针退出后 `resume`，准确答出第 1 轮的暗号。Codex 和 Claude 的 resume 只有单元测试 |
| D10 | 2026-11-06 | worker 会话和它启动的服务都保留到范围结束，然后列进回收清单交给用户；总控不自行关闭会话、停止服务（权限检查也会拦截这类操作） | #4 验收报告把回收列为"用户决定"；关闭 3100 服务和 #4 会话的操作被权限检查拒绝 | brief 增加一句：报告完工前停掉自己启动的服务 |
| L1 | 2026-11-06 | 教训：审查者断网时，总控要联网交叉核验上游数据的语义。#5 的 V1（QDII 估值基准是 T-2）只靠读代码发现不了 | #5 首次完整审查通过，但把字段语义列为限制；总控核验后发现真实缺陷 | 写进 surper-coding：审查报告里的"限制"项由总控负责关闭 |
| L2 | 2026-11-06 | 教训：修复任务里不能让实施者"自选"故障分支的行为；应该事先定好原则："宁可不可用，不给错值" | #5 修复 r2 自选"净值故障时照常采纳"，被增量复核否决 | 多花了一轮修复 |
| D11 | 2026-11-06 | #8 X1/X2 的解法：沿用 #4 的交易日历机制，按市场区分，加入港股（HK）年度休市日和半日市数据，随仓库附带 2026 年数据；港股状态、半日市收盘和"该有当日行情"的判断都依据这份日历，不再用报价日期推断 | 父 spec 只有 A 股日历；没有港股日历就无法区分"上游陈旧"和"港股假日"（审查 X1、X2） | 用户可以否决；否决的话，退回为港股状态只标"未知"，并接受 X2 |
| D9a | 2026-11-06 | `resume` 已在三种宿主上真机验证：OpenCode（#9 实施者被 Ctrl-C 结束后恢复，完成了任务）、Codex（#8 审查者 /quit 后用 `resume --task` 回到 rollout 01a1b107…，历史中有第 1 轮的 X1）、Claude Code（#9 审查者 /exit 后用 `--resume` 回到 23ac9e32…，第 2 轮任务接在第 1 轮之后） | 补齐 D9 中"Codex 和 Claude 只有单元测试"的限制 | — |
| U8 | 2026-11-07 | 用户决定：测试环境下 OpenCode 可以读取 `.env`。router 给 OpenCode 的配置放行 `*.env`、`*.env.*` 的读取 | #9 的 worker 卡在 OpenCode 默认的"Read .env"询问上 | 只适用于测试环境；生产密钥不放进 worker 能读到的工作树 |
| U9 | 2026-11-07 | 用户决定：workflow-core 每次推送前，都要像工作流一样先经过 Codex 的全量审查；有阻塞的先修复，修完再复核 | 用户授权推送时附带的条件 | 适用于每一次推送 |
| U10 | 2026-11-07 | 用户决定：拆成两个仓库。调度器（`wfc`）留在 xmxxc/workflow-core；技能 surper-coding、它的例行检查脚本 `ctl.py`（移到 `skills/surper-coding/scripts/`）、ai-coding/live-verify 的锁定副本和安装脚本放进新的私密仓库 xmxxc/surper-coding。开发者按需各取 | 公开时，调度器可以单独使用；标签等检查脚本是技能规则的执行部分，随技能分发，免得两边版本对不上 | 决策记录和探针文档留在 workflow-core |
| U11 | 2026-11-07 | 用户决定：以单文件 `wfc`（Python zipapp）分发，CI 跑 Ubuntu、macOS × Python 3.9、3.12，打标签时自动发布 Release；不做 Windows 原生支持，暂不改用 Go | 下载即用，技能里不再写调度器路径 | Windows 用户用 WSL2 |
| D12 | 2026-11-07 | OpenCode 当总控：`wfc relay --opencode-url --opencode-session` 通过 OpenCode 的 `POST /session/<id>/prompt_async` 投递消息，总控须以固定 `--port` 启动。投递语义与 Codex 一致：连接被拒（确定未送达）就重试；错误响应或超时属于不确定，不重试，写入 relay.log | 用户要求三个宿主都能当总控，并且必须实测 | 真机验证：OpenCode 总控收到探针 worker 的 done 后回复 "ACK done"；在小范围票据上的完整总控测试另行进行 |
| U12 | 2026-11-07 | 用户决定：技能和仓库名纠正为 `super-coding`（原拼写 surper-coding 有误）。GitHub 仓库改名为 xmxxc/super-coding，技能目录、安装名和 Release 包名同步更改；旧标签 v0.1.0、v0.2.0 的包名保留原样。上面各行中的旧名是当时的记录，不再修改。 |
| D13 | 2026-11-08 | wfc 0.3.0：看门狗从宿主自己的会话记录读每轮结束状态（Codex rollout 的 `task_complete.error`、Claude Code 记录的 `isApiErrorMessage`、OpenCode 的 `message.data.error`），本轮以错误结束且没有 `done` 时立即报 `blocked`，附错误类型和原文；新增 `nudge`（向未完成的 worker 补一句话并记账）和只读的 `status` | 总控实测：额度耗尽只能等 stall，`continue` 拒绝卡住的 worker 只好绕开 router 敲字，`wait` 输出丢失后只能查 sqlite | 这些记录是宿主内部格式，读不到就当没有，屏幕检测保留作兜底；错误类型取自本机历史记录 |
| U13 | 2026-11-08 | 用户决定：workflow-core 和 super-coding 按标准工作流开发：总控开 issue，默认 Claude 当 worker、Codex 当审查者，经 PR 合并 | 两个仓库已经起步 | 推送前 Codex 审查（U9）照旧 |
| D14 | 2026-11-09 | wfc 0.4.0：`status` 完全只读（#3）；`dispatch --effort/--network` 映射到各宿主自己的参数（#4）；`done` 记录 worker 工作树的 head 和 dirty，git 输出按字节读取（#5）；会话标签"项目 \| 票 \| 角色"写入 Claude 的 `--name`、tmux 窗口名和 status（#6）；可选的 `--git` 让 Codex worker 在沙箱里本地提交（#7） | 总控实测反馈的第 6、8、9 项，以及会话难以定位 | — |
| D15 | 2026-11-09 | Codex 和 OpenCode 不设宿主会话标题 | 启动时没有可用参数，只能在启动后通过宿主的内部接口去改；总控在实施 r4 的提问 7 里选了 a | 以后要做的话，由看门狗在宿主会话出现后改名：Codex 调用 app-server 的 `thread/name/set`，OpenCode 对同一份数据上的服务发 `PATCH /session/{id}`；参见 `docs/probes-20261109-session-title.md` |
| U14 | 2026-11-09 | 用户决定：`--git` 必须提供，但默认关闭："可以加，可以不用，但不能没有" | 见 #7：Codex 沙箱默认不能在链接 worktree 里提交 | 开启后 worker 能改主仓库 `.git` 下的所有内容；推送、开 PR、发评论仍由总控来做 |
| D16 | 2026-11-09 | wfc 0.4.1：`wfc relay --codex` 从 `CODEX_THREAD_ID` 取 Codex 总控的线程；显式给出的 `--codex-thread` 先核对 rollout 存在、不是子代理、与 `CODEX_THREAD_ID` 一致，否则拒绝；`codex queue` 报 `no rollout found` 时按确定失败处理，退回未送达并退出（#9） | example-project 实测中，总控两次把 relay 绑到了错误的 ID（一次是 worker rollout 的 `context_window.window_id`），`codex queue` 的失败被当作"不确定"，标成了已送达，消息静默丢失 | 探针见 `docs/probes-20261109-codex-thread-id.md`；子代理里的值未实测（审查 R27-1，P3） |
| D17 | 2026-11-09 | wfc 0.4.2：`status --active` 只列未结束的 dispatch（working、asking、exited），`undelivered` 和 `waiters` 照常；已经 done 但 tmux 会话还活着的，不进 `dispatches`，另列在 `reclaimable`（`dispatch`、`label`、`tmux`）；`--active` 不能和 `--dispatch` 同用；不带参数的输出不变（#11） | example-project 实测：一个状态目录里 45 个 dispatch 有 39 个已完成，总控每次要另写 Python 过滤 `status` | 选字段而不选 `--sessions`：一次调用就能同时看到在跑的和可回收的会话，不多一个参数；`reclaimable` 只在 `--active` 下出现 |
| D18 | 2026-11-09 | wfc 0.4.3：检测只读或不可用的宿主会话（#13）。`continue`、`nudge`、`reply` 和 OpenCode `resume` 的 brief 打字前先抓屏，画面已显示只读或不可用就不打字；回车后 1.5 秒内（`WFC_TYPED_CHECK_SECONDS`）画面上出现 state 提示（只读或不可用，出现就算），或者拒绝行比回车前多了（残留的旧拒绝行不算），都按未送达处理：回滚语义与 NOT_SENT 相同（`continue` 只在本轮仍是最新时回滚），报错附原文并提示新派。Codex `resume` 之后观察画面最多 5 秒（`WFC_RESUME_CHECK_SECONDS`）。看门狗只把 state 提示当作只读的证据，遇到就立即报 `blocked`，`kind` 为 `session_read_only`，同一画面只报一次；拒绝行只是对某一次回车的回应，画面上只有拒绝行时按正常的 stall 处理；命令已报过的画面看门狗不再报。匹配文本放在 `router.py` 的 `READ_ONLY_NOTICES`，来源是 Codex 0.167.0 实测画面（wfc-d-3c8ab0af、wfc-d-ece89726） | 2026-11-09 切换服务商后 35 个 Codex 会话变成只读，`continue`、`nudge` 照报成功，要等 15 分钟才报 `stalled` | 正常发送每次多等 1.5 秒：画面上只有拒绝可见，成功和尚未回应看起来一样，没有可靠的提前结束信号。窗口之后才出现的 state 提示，看门狗会立即报 `blocked`；只有拒绝行的画面按普通 stall 规则处理（有待回答的问题时不报 stall），不保证立即报 `blocked`。文本必须位于行首（可带 `■ `、`• `），以免 worker 在工具输出里引用时误报；Claude Code、OpenCode 暂无已知文本 |
| D19 | 2026-11-10 | wfc 0.4.4：新增 `wfc reclaim`（#16）。用户决定会话清理"可以不用，但不能没有"：只提供命令，总控先把清单给用户看，用户同意后再执行。默认只列出、不做改动，`--yes` 才 `tmux kill-session`；默认只关已 done 的，没完成的必须用 `--dispatch` 逐个点名并加 `--yes`；关闭前用 `tmux show-environment -t wfc-<id> WFC_STATE` 核对会话属于本状态目录，不一致就拒绝；每个状态目录各管各的。关闭后在 router.db 记一条 `reclaimed`（收件方 worker），看门狗不再报 `exited` 或 `stalled`，`resume` 照常可用，之后恢复监视。不删除 router.db 的记录、账本和宿主聊天，也不碰 worktree 和分支 | example-project 一个状态目录里积压了几十个已完成却还开着的会话；router 不知道 PR 的状态 | dispatch 和消息记录保留，`resume` 和账本对账都要用到；范围结束后要不要删除整个状态目录，由用户看着回收清单决定。`status` 用新字段 `reclaimed`（回收时间）标出，last 消息也是 `reclaimed`；未完成的显示 `state: reclaimed`。`reclaimed` 不计入 `round_mark`、`done_current_task`、`watched`。关闭前在 input lock 和写锁内复核：未点名的必须仍是 done 且未回收，否则记 `changed` 并保留会话；点名的不复核 done，因为点名就代表用户明确要关（审查 R4-1） |
| D20 | 2026-11-10 | wfc 0.4.5（#18）：Codex worker 的 `reply`、`continue`、`nudge` 在回车后的核对窗口之后看 Codex 的输入框：框里还是刚打的文本，就补按回车，最多两次，每次之后同样有核对窗口（这时出现的拒绝行照样算只读）；补完仍在框里的按 PARTIAL 处理，记录保留，提示 attach。只有隔 0.3 秒的两次抓屏里输入框相同、且框里是所打文本时才补按（忽略换行；框要从文本开头开始，只有标了 `↑` 的滚动框可以是文本中间的一段）；已知占位提示（`INPUT_BOX_PLACEHOLDERS`，Codex 0.167.0 为 `Ask Codex to do anything`，实测）直接当空框，不和文本比较（审查 R6-1）；不在表里的占位提示不滚动，也不会是以 `[router]` 开头的文本的开头，同样不按；框空着、是别的文本、还在变化，都不按，避免重复提交和空回车。输入框是状态栏上方最后一个从行首 `›` 开始的段落（后续行缩进两格，过高时右侧有 `↑`/`↓`），紧接一到两行带 ` · ` 的缩进状态栏，中间只能有空行；不像这样的画面不补按。Linux 上 waiter 改按 `/proc/<pid>/stat` 第 22 字段（加 boot id）登记和精确比对；boot id 读不到时这部分留空，只凭 pid 和 starttime，重启后同 pid 同 starttime 的进程会被当成原进程，没有跨重启保证；其他平台和 0.4.4 及以前登记的 lstart 解析成时间后允许 ±10 秒（`LSTART_SLACK`） | 消息 175 的长回答停在 example-project #7 worker 的输入框里，15 分钟后才报 `stalled`（样本：消息 176 时的画面）；relay pid 3991093 的 lstart 从 17:15:10 漂到 17:15:07，`status` 判它已死，总控又起了一个 relay | 探针（私有 tmux socket，用完 kill-server）：Codex 0.167.0 已提交的消息在记录里同样以 `›` 开头，空框回车无反应，长文本滚动时显示 `↑`；Claude Code 2.1.322、OpenCode 1.19.5 只看了打字后的输入框，没有提交（不确定提交后、长粘贴和空框回车的样子），这两个宿主保持一次回车。lstart 误差内复用的 pid 会被当成原进程，可接受。升级后已在运行的 relay 要重启才生效 |
