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
