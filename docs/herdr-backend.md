# Herdr 执行后端

WFC 继续持有 dispatch、round/attempt、消息和 owner fencing；Herdr 负责终端与进程。使用用户单独安装并运行的 **Herdr 0.9.5 / protocol 22**，通过一个公开 NDJSON API 入口通信，不嵌入 Herdr runtime。固定依据为 [Herdr 源码](https://github.com/herdrdev/herdr/tree/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde)；其他版本明确拒绝，升级需重验协议。

## 运行

```sh
herdr server
python3 wfc.py dispatch --backend herdr --herdr-endpoint /absolute/private/herdr.sock \
  --host codex --task task.md --cwd /absolute/work --report report.md \
  --launch-config /absolute/launch.json
```

Windows 使用 `python wfc.py` 或 `python wfc.pyz`。`--herdr-endpoint` 为 Herdr 的本地 pipe 名称（不含 `\\.\pipe\` 前缀）；Unix 为绝对 socket 路径。也可使用 `WFC_HERDR_ENDPOINT`。启动 Herdr 时用其 `HERDR_SOCKET_PATH` 指向同一入口。Herdr 服务应属于当前用户；Unix socket 必须为当前用户所有且没有 group/other 权限。Windows 使用 Herdr 的受保护本地 pipe 和只读 server PID 查询。

后端显式选择，默认仍为 tmux；已有 dispatch 持久保存后端和 endpoint，恢复不根据 PATH 或 OS 切换。缺 endpoint、连接失败、版本不匹配和不可观测身份均返回诊断。Codex/Node 的实际路径用 [launch config](host-launch.md) 配置；Windows npm 的 `.cmd` shim 应改为 JS 脚本加 Node runtime argv。WFC 不安装依赖、不改变 PATH，不替宿主处理登录或权限确认。

## 身份、输入和恢复

创建前先提交 pending lifecycle 与私有启动收据；分阶段保存 Herdr service PID/creation identity、workspace、pane、terminal、ownership nonce 和 WFC attempt。服务在每次 API 请求之间重新校验身份。每次单元操作读取完整 session snapshot，核对 workspace nonce 与 pane/terminal；不存在事件订阅游标，因此断连后不用缺失事件推断成功。read 返回 revision 和截断标志，终端文本不是消息确认依据。

`submit` 成功仅表示 Herdr 接受输入。任务完成仍需 worker 的 `done`。超时、断连和不完整响应保留 `uncertain`，不会自动重放；只读或 blocked 宿主拒绝发送。interrupt 的 accepted 不表示 writer 已停止。close 仅关闭该 dispatch 的 pane，不关闭共享 sibling；必须用启动收据或 PID/creation identity 确认旧 writer 停止后才返回 completed。恢复创建新 attempt 前再次核对旧 writer；新 worker 在 pending resume/handoff 期间可持久提交精确匹配该 attempt 的问题，但轮次保持 unknown，回答仍需等待启动确认；旧 attempt 的回调继续被核心拒绝。

服务重启会保持 unknown，不能仅凭相同 pane 字符串接替。操作者先检查 Herdr snapshot 与进程，再显式绑定已有归属单元：

```sh
python3 wfc.py herdr-reconcile d-... --pane p-... --terminal t-... \
  --actor operator --evidence '实际检查的单元与归属证据' \
  --old-writer-evidence '旧服务/writer 已停止的实际检查证据'
```

`--workspace` 可指定恢复后的 workspace；其 nonce 必须与持久记录一致。service identity 改变时必须提供旧 writer 证据；无法观测全部 writer 身份时保持 unknown。单元缺失时只有显式停止证据才能标为未运行。绑定不发送输入、不创建新 attempt，也不清除未决输入或 lifecycle；这些仍通过既有 `reconcile` 命令逐条审计。owner scope 必须传当前 `--owner-token`。审计保存 actor、证据和身份，不能把示例证据当作实际检查。

## 验证与范围

`WFC_HERDR_TEST_BINARY=/path/to/herdr python3 -m unittest discover -s tests -p test_herdr.py -v` 使用隔离的真实 Herdr 服务和受控 Python 宿主，覆盖问答、done/continue、interrupt/resume、迟到回调、owner 接替、共享 pane 回收、只读拒绝、服务重启和 Unix 丢响应故障。CI 在 Linux 与原生 Windows 执行此合同；测试下载工具只下载校验固定 SHA256 的官方二进制到新目录，不做全局安装。

受控宿主结果不等于真实 Codex 验收。真实 Windows Codex 的登录、权限与交互闭环由 #10 保留独立验收记录；不能据此宣称 live-verified。Unix tmux 保持原支持范围。生产启用持续 watchdog 前还需合入 #19 的故障去重修复。

已确认的上游边界：公开 CLI 没有 `layout.apply` argv 入口，因此选择公开 API；每个连接只处理一个请求，适配器逐请求建连。Herdr 0.9.5 重启恢复的单元可能不保留 metadata nonce；此时拒绝绑定，需人工确认并停止旧 writer 后另建显式 dispatch，不能仅以相同 ID 接替。API 没有原子“核对 nonce 并写入”操作，适配器在同一 service identity 内先 snapshot 后操作；拥有同一 OS 账户权限的外部操作者仍可并发更改单元，应在管理窗口避免并发手工修改。

## 宿主 shell 的回调上下文

Codex 0.169.0 的 Linux 实测中，主进程已收到正确 WFC 环境，但其生成的 shell snapshot 带有上一个 dispatch/state，回调因 attempt 不匹配而被拒绝。WFC brief 的 ask/done 命令现在显式设置当前 `WFC_DISPATCH` 和绝对 `WFC_STATE`；Unix 以引用后的 `env` 参数传入，Windows 以 PowerShell literal 设置后启动 Python。不依赖宿主 shell snapshot 的这两个环境值，不改变宿主全局配置。owner token 仍从继承环境读取并校验；失效凭据拒绝，不在 brief 中嵌入凭据或绕过 owner fencing。

## Codex reply 的 Enter

Herdr 0.9.5 的 `pane.send_input(text, keys=[Enter])` 将 paste 和 Enter 合在同一批字节中；API 接受不保证 Codex 已提交。Codex 普通输入现在在发送后使用现有 composer parser 读取两次画面，相隔 300ms。只有两次显示相同、完整的本次文本时才补 Enter，最多沿用 `ENTER_RETRIES` 的两次上限；每次补键后重新观察。已清空、已提交、其他草稿、仅前缀重合、滚动裁剪、无法识别或正在改变的输入框不授权补键。长回复若只能看到部分内容，仍需显式检查；这里的 accepted 继续只是后端接受回执。

补键沿用 service identity、workspace nonce、pane/terminal fencing 和 blocked 检查，整个 submit 持有现有 dispatch input lock。初次发送未知时立即停止；发送文本后的读取或补键失败、单元消失、只读提示或补键后仍保留完整草稿时返回 uncertain，保留 reply claim，不自动重打文本或重试未知的键。此适配只用于 Codex 普通输入。受控 raw TUI 的公开 ask/reply 回归将合并 Enter 丢弃，并单独记录宿主 user 事件；它不证明原生 Windows Codex 或无人值守 A 已通过。

Windows A/D 短复验：每项独立配置 endpoint、`HERDR_CONFIG_PATH`、所有 XDG 目录和 `WFC_STATE`，记录 Herdr/Codex/Python 版本。A 对 worker 的 ask 调用一次公开 reply，不补人工键，确认同一 worker 恰好一条匹配 user 消息；随后 done 必须经 detached relay 唤醒同一 Desktop 聊天且 ack 为 processed。D 接替 owner 后，旧凭据继续被拒绝，新 reply 只进入原 worker 一次且无人工补键；保留旧 owner done 拒绝语义。A 的 detached relay 和 D 的接替完成路径仍分别受 #31、#4/#6 约束，不能从本项 reply 回归推定通过。

## OpenCode 2.0.12 首次 prompt

Herdr 首次启动 OpenCode 后，WFC 使用 5 秒观察窗口读取其 composer（单次 RPC 另受现有超时限制）。仅当两次相隔 300ms 的画面都能识别同一个输入框，且可见文本是本次 argv brief 的完整内容或末尾，且包含本次 attempt时，补发一次 Enter；不重打文本。已提交、显示其他内容、无法识别或出现只读/blocked 提示时不补键。补键仍使用原 service/nonce/pane fencing，发送结果未知时保留 uncertain。创建过程持有 dispatch input lock，使 nudge/reply 与初始提交串行。

OpenCode 2.0.12 的会话存储使用 `session_v2` / `session_message`。WFC 按实际表结构读取，兼容旧 `session` / `part` / `message`，只从顶层会话的 user brief 找到归属。新版错误需由终止 turn 的 failed idle 与同一轮 assistant error 共同确认；新用户输入、流式重试和成功恢复不重报旧错误。WFC 不修改 OpenCode 数据库或执行其迁移。

Windows 用户已回传 OpenCode worker 与 Codex controller 的实际消息闭环，见 [实测记录](verification/windows-opencode-20261115.md)。该包首派曾需人工补 Enter；上述修复需在新的候选包上另做 Windows 复测。此适配不构成对 OpenCode 2.x 所有 CLI 参数和宿主版本的普遍支持声明。
