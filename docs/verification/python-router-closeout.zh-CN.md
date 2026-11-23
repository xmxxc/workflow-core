# Python 路由器开发基线与 Linux 真机验收

截至 2026-11-23，Python 候选为 `60d7d76e4bb861ac9e0bbb043aa1a36946578397`，tree 为 `a401334e1525b236b654e30385f4ee216c2f9c5e`。相对 main `3c06c0905faaf9e61de49404c33e70720409607f` 领先 7 个提交、落后 0 个。组合草稿 PR 完整包含两层候选，产品开发和 Windows 验收已完成，合并与稳定版发布由维护者决定。[组合 PR #45](https://github.com/xmxxc/workflow-core/pull/45) · [基础 PR #39](https://github.com/xmxxc/workflow-core/pull/39) · [增量 PR #40](https://github.com/xmxxc/workflow-core/pull/40)

两个未参与实施的会话审查了该提交相对 main 的完整差异，分别核对实现和规格，均为零项产品阻塞。采集器有一个已知限制：私有状态删除失败时仍可能以 0 退出，验收须单看 `cleanup.private_state_removed`。组合 PR 的常规矩阵 8 项全过，Windows 原生作业在产品提交上完成构建及源码、解压包测试。Linux 和 macOS 的常规 CI 结果可随运行记录查看。[范围盘点](https://github.com/xmxxc/workflow-core/issues/41#issuecomment-7089980490) · [独立审查](https://github.com/xmxxc/workflow-core/issues/42#issuecomment-7090084977) · [常规 CI](https://github.com/xmxxc/workflow-core/actions/runs/45100606361) · [原生 CI](https://github.com/xmxxc/workflow-core/actions/runs/45100606373)

## Windows 下载与已测范围

使用 GitHub 账号下载工件，解开外层后核对内层 ZIP，再将其中的整个 `wfc` 目录解压。需要 Windows x64、PATH 上的 Python 3.9+ 和独立安装、已登录的宿主 CLI。执行 `.\wfc\wfc.cmd --help` 检查入口。下载有效至 **2026-12-07 02:36:28 UTC**。[Windows 测试包](https://github.com/xmxxc/workflow-core/actions/runs/45100606373/artifacts/14785243240) · [下载、复验与逐件比对](https://github.com/xmxxc/workflow-core/issues/43#issuecomment-7090155489)

| 交付件 | SHA-256 |
| --- | --- |
| 内层 Windows x64 ZIP | `d9b9e609b5bd949694c51241fef5b4262c8754e8531ae4a12282fa18298f3adf` |
| `wfc.pyz` | `7f73d9763ceb4768fd158c15bd251dd98bf0ae72bf077196f9b58d0d02c721c6` |
| `wfc-kernel.exe` | `4f7d15dba0a325b33195f64d5111631848164ad21f78957ab8bdb69420a4b717` |

- **下载件。** Win11 在两个新目录（其中一个含中文和空格）运行 bundled 套件，各 22 项通过、8 项平台跳过。受控接替中同一 PID 和 attempt 完成 ask 与 done。真实 worker 使用独立原生 Codex CLI 0.164.0、`--no-daemon`、`gpt-6.1-sol / high`、`workspace-write` 和 `TERM=xterm-256color`。经 `--backend bundled --host codex` 派发后，一次公开 reply 在原会话仅产生一条正式用户消息。报告匹配，done 已 ACK，签名清理回执、四个进程身份退出、waiter 为零和私有状态删除均已核对。[下载件现场](https://github.com/xmxxc/workflow-core/issues/43#issuecomment-7090155489)
- **两个真实 Desktop 聊天。** 沿用 #10 的 Win11 记录。B 接替 A 后原 worker、会话、attempt 和 backend generation 保持一致。B 的一次 reply 产生一条正式消息和一条对应事件；B 各收到一次 question 和 done，A 两项均为零。旧 A 的 wait、nudge、done ACK 均被拒绝。B 的重复 ACK 保持版本 4，两个 relay 均退出。与当时的包逐项比较，交付件的七个 Python 模块仅换行不同，原生 EXE 仅时间戳和调试 GUID 改变。首次启动失败和首次回复停在输入框的现场分别留存。[双聊天实测](https://github.com/xmxxc/workflow-core/issues/10#issuecomment-7089825806) · [接替操作与证据字段](windows-bundled-takeover.zh-CN.md)

## 留给 Linux 真机总控的验收清单

准备同一产品提交、按固定上游构建的 Linux 原生 `wfc-kernel`、Python 3.9+（含 SQLite）、可交互且已登录的 Codex CLI，以及能分别接收 relay 通知的两个真实聊天。记录系统、Python、宿主版本、候选/companion 哈希和启动参数。使用受信任的临时工作目录，为每次尝试分配独立的 `WFC_STATE`、scope、任务和报告路径。保留首次失败，重试另建目录；外发结果不明时保留现场，避免重复回答。

先在一次性的 `systemd --user` 单元中启用 `Delegate=yes`，确认委派的是有 `cgroup.kill` 的可写 cgroup v2 子树。运行 `tools/verify_bundled_linux.sh REPOSITORY KERNEL LOG_DIRECTORY`，检查源码及搬迁包日志和子 cgroup 清理。真正派发时，在启动服务的环境中将 `WFC_BUNDLED_CGROUP_ROOT` 指向本次委派子树。试验结束由总控停止本次单元。[平台契约与构建入口](../bundled-backend.md) · [受控验证脚本](../../tools/verify_bundled_linux.sh)

| 真机路径 | 通过条件 |
| --- | --- |
| 1. 派发 | A 先注册本聊天的 relay，再用 bundled/Codex 派发。记录 dispatch、round、attempt、backend generation、worker PID 及创建身份、宿主会话；worker 在目标目录读取本轮任务。 |
| 2. 原聊天唤醒 | A 结束当前 turn，worker 提问后由 relay 自动唤醒原聊天。核对聊天投递记录与账本中的同一 messageId，避免另起 wait 消费通知。 |
| 3. 问答和完成 | A 对收到的问题公开 reply 一次；原 worker 会话有且仅有一条匹配的正式用户消息。报告内容和路径匹配 attempt，done 的 messageId 经 ACK 成为 processed。 |
| 4. 接替 | 在新现场让 B 接替在途任务。owner generation 加一，原 worker、会话、attempt 和 backend generation 保持。B 的一次 reply 被原 worker 正式消费一次，B 各收到一次 question/done，A 各收零次。旧 A 的 wait、nudge、done ACK 均被拒。 |
| 5. 重复回执 | B 对同一 done 的首次 ACK 到 processed；再次 ACK 标记 duplicate，messageId、状态版本和报告保持一致。核对账本中仅有一个逻辑 done。 |
| 6. 清理 | 由当前 owner 关闭本次 dispatch。保存签名回执，核对 worker、service、kernel、guardian 和 relay 的 PID/创建身份已退出，waiter 为零，试验子 cgroup 最终移除。保留脱敏证据和每次运行结果。 |

Linux 真机六项由维护者另起总控执行。Rust 可复用表中的输入、身份字段与通过条件做行为对照。
