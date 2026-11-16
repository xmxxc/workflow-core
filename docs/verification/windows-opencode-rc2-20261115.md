# v0.4.7-rc.2 Windows 用户实测通过 — 2026-11-15

来源：[用户回传与 GitHub 记录](https://github.com/xmxxc/workflow-core/issues/10#issuecomment-6895281052)。本记录对应已发布的 rc.2 包；后续 #28 修复候选需单独在 Windows 复验。

用户回传，非本机代验。WFC commit `90b384dd56bbe3287a49877dcde63161729016af`；ZIP SHA256 `f267f84673823f94268303efe49c1aaa7b195f9ed3c4750de87594e6f344a866`，与 SHA256SUMS/release digest 一致。Python 3.13.12、Herdr 0.9.5、OpenCode 2.0.12；relay 为桌面 Codex 0.169.0-alpha.11.1，通过进程 PATH 指定，未改全局 PATH。

dispatch `d-9929b3d3`，scope `s-fe3f1e5c`，独立 endpoint `wfc-rc2-probe-20261115`。任务/状态/报告保留在用户临时测试目录 `wfc-rc2-probe-20261115`。

- 第一轮 `a-e85be4728468`：无人工补 Enter 首派成功，question `WFC_RC2_READY`，reply ready，done；result.md 含 `WFC_RC2_PASS`。question 1、done 3 已 ack processed。
- 正常 /exit 后 exitCode 0、backendAlive false；为执行退出菜单项补 Enter，与首次任务自动提交无关。
- 第二轮 `a-2bbcd94bdcaa`：resume accepted，恢复同一 session `ses_e20921b26ffeZTx1blq7cSkPLL`，resume.md 含 `WFC_RESUME_PASS` 且准确回忆上一轮问题。done 6 processed；无 `no such table: session` 警告。
- 第三轮 `a-2fe9d36120b9`：continue 后 relay-300.md 含 `WFC_RC2_RELAY_300_PASS`，实际计时 300.011 秒。用户报告开始 2026-11-15 21:40:42.214 +08:00，结束 21:45:42.234 +08:00；router 21:45:49 记录 done。
- relay 参数 `--codex --scope s-fe3f1e5c --for 420 --stall-after 420`；原始消息进入同一聊天并实际触发回合，核对身份/报告后 ack `m-9c78884da38b463f86e836ee4bdcacc5`，processed、statusVersion 4、duplicate false。
- close 返回 completed；核对 creation identity 后停止专用 relay PID 23796，并停止专用 Herdr。包、数据库、任务与报告保留。

新发现：close 将已完成第三轮改为 cancelled，done/processed 仍保留，单独追踪 #28。

未覆盖：终态模型错误注入、崩溃恢复、Herdr 重启、owner 接替、并发故障；Windows Codex worker 仍无本次证据。#9/#10 保留剩余验收，不整体关闭。rc.1 的人工 Enter、数据库警告和仅15秒延时属于历史记录，不能继续描述成 rc.2 待修项。
