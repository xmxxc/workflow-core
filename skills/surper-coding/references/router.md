# router 派发

用 workflow-core 的 `router.py` 把 worker 和审查者派到各自宿主的 tmux 会话里，然后你去睡眠，等它们发来消息再醒。账本、交接、审查、验证和放行的规则见 [ai-coding 的 references](../../ai-coding/references/)。

## 前提

- **router 位置**：先用用户本次给的 `router.py` 路径；没给就用账本里记录的；两处都没有，就问用户一次。
- **状态目录**：每个范围用一个独立的 `WFC_STATE`，例如 `~/.local/state/workflow-core/<仓库>-<范围>`。对这个范围执行的每条 router 命令都要带上它，并把它写进 `state.md`。
- **同一个 tmux 服务**：每个 worker 在名为 `wfc-<dispatch id>` 的 tmux 会话里运行。总控、worker，以及用户执行 `tmux attach` 时，都要连到同一个 tmux 服务（同一个用户、同一个 `TMUX_TMPDIR`）。看门狗找不到会话，就会报 `exited`。
- **信任**：
  - Codex 的信任作用于 git 仓库根。接管时请用户在仓库根运行一次 `codex` 并选择信任，之后该仓库的所有 worktree 都不会再问。
  - Claude Code 会对每个新路径单独询问。如果用户已经信任过某个上级目录，router 派发时会替这个 worktree 写入信任；派发结果里的 `trustedBy` 就是那个上级目录。
  - 除此之外，信任由用户亲自确认。worker 卡在信任提示上时，看门狗会报 `stalled`。

## 派发

```sh
WFC_STATE=<状态目录> python3 <router.py> dispatch --host <codex|opencode|claude> [--model <模型>] \
  --task <prompts/ 中任务文件的绝对路径> --cwd <工作树> --report <reports/<票>-<角色>/r<N>.md>
```

- **回报目录按会话划分**：同一个 dispatch 的各轮回报都放在一个目录里，例如 `reports/2-impl/r1.md`、`reports/2-impl/r2.md`。router 只在派发时把这个目录开放给 worker 写入，所以 `continue` 也只接受这个目录里的路径。
- **派给 OpenCode 时必须带 `--model`**，因为它的默认模型可能是受限的免费模型。
- **无人值守下的权限**：
  - Codex：`workspace-write` 沙箱，不请求审批，默认不能联网；
  - OpenCode：工作目录外的路径一律放行，没有目录限制，可以联网；
  - Claude Code：`auto` 模式，可以联网。
- 需要推送分支、建 PR 或发评论的任务，派给能联网的宿主；或者在你的授权范围内由你代发。
- worker 完工用 `done` 报告，需要决定时用 `ask` 提问。这两条命令的用法由 router 写进 worker 的说明。

### 断网的审查者（Codex）

派发之前，先准备两样东西，都放进它的回报目录：

- 在固定快照上执行项目的全量检查（安装、类型检查、lint、格式、测试），保存日志，并记录每条命令真实的退出码；
- 需求来源，也就是 Issue 和 spec 的正文。

审查结论写进报告，标为"待发布"。由你注明原作者后，转录到 PR 上。

### 状态与证据

| 状态 | router 证据 |
| --- | --- |
| `pending-handoff` | `dispatch` 或 `continue` 已经成功返回 |
| `accepted` | 收到该 dispatch 的消息，或者 `tmux capture-pane -p -t wfc-<id>` 显示它已经开始处理本轮任务 |
| `returned` | 收到本轮的 `done`，并且按 [账本](../../ai-coding/references/ledger.md) 完成了回读核对 |

**独立性**：实施和审查分别派发。dispatch id 不同，就是不同的宿主会话、各自的上下文。各个 worker 能读到同一个状态库，OpenCode 还能写任意路径，所以文件隔离不能作为独立性的证据。审查者的 `--cwd` 用固定快照或独立的工作树。

## 续派：修复与增量复核

```sh
WFC_STATE=<状态目录> python3 <router.py> continue <dispatch> --task <下一轮任务文件> --report <同一回报目录中的 r<N+1>.md>
```

- **修复**：审查有阻塞时，用 `continue` 把修复任务交给原来的实施者。任务文件里写明审查记录的链接、要修的各项、当前 head，以及"在现有提交上追加，不要 force-push"。
- **增量复核**：修复报告 `returned` 之后，把审查快照移到新的 head，在快照上执行全量检查，然后用 `continue` 让原来的审查者复核"上次获审候选 → 新 head"。
- **何时可以续派**：只有在该 dispatch 已经对当前任务报告 `done` 之后才行。`continue` 发现上一轮还没有 `done`，会直接报错。
- **何时不能续派**：会话已经结束（`exited`），或者原会话不再合适（例如宿主配置已经改了），这时就新派一个会话，并在任务文件里写明真实的起点。会话已经结束时，`continue` 会直接报错，不会把任务打进去。

## 等待与唤醒

**Claude Code 总控**：每次派发、续派或回复之后，在后台挂上

```sh
WFC_STATE=<状态目录> python3 <router.py> wait --timeout <秒> [--stall-after <秒>]
```

然后结束当前回复。同一时刻只保留一个 `wait`。用户在窗口里发言也会唤醒你；回应完用户后，确认后台的 `wait` 还在。`wait` 以退出码 3 返回，表示超时：回读各任务的现场，有进展就重新挂 `wait`，没有进展的任务记为 `blocked`。

**Codex 总控**：总控会话用 `danger-full-access` 启动。接管后执行一次

```sh
WFC_STATE=<状态目录> python3 <router.py> relay --codex-thread <你的会话 UUID> --detach [--stall-after <秒>]
```

它会输出进程号和日志路径，把它们写进 `state.md`。会话 UUID 的找法：在 `~/.codex/sessions/` 下找包含本会话启动提示原文的 rollout 文件，取第一行的 `id`。每次派发或回复之后结束当前回复，worker 的消息会作为一条以 `[router] Messages for the controller:` 开头的新消息送到你这里。全程不要运行 `wait`：`wait` 和 relay 同时运行，会抢同一批消息。另外，`codex queue` 对不存在的会话 UUID 也会返回成功，所以 relay 启动后，要确认它投递的第一条消息确实到了你这里。如果派发后迟迟没有动静，先在 `relay.log` 和 `router.db` 里看消息是否已经标为送达。

## 消息处理

| 消息 | 处理 |
| --- | --- |
| `done` | 完成 `returned` 的回读核对。缺留痕的，按账本补发转录，或者交回 worker 补发 |
| `question`（由 `ask` 发出） | 在已固定的范围、验收条件和授权内能回答的，直接执行 `router.py reply <消息 id> "<答案>"`。需要用户决定的，把原问题和选项原样转给用户。问题和答案都记入账本 |
| `stalled` | 先读附带的画面。<br>停在信任或权限提示上：请用户执行 `tmux attach -t wfc-<id>` 处理，该任务记为 `blocked`。<br>停下了，但没报告也没提问（例如模型调用遇到网络错误）：回读现场，然后用 `tmux send-keys -t wfc-<id> -l "<提醒>"` 打一行提醒，隔半秒再发 `Enter`，并把这次介入记入账本。提醒之后再次报 `stalled` 的，记为 `blocked` |
| `exited` | 先检查 worker 退出前是否已经提交或推送，然后按账本的失效规则处理：记为 `stale`，保留现场，用下一轮编号新派，在新任务文件里写明真实起点，以及旧现场是续用还是放弃 |

每条消息都带 dispatch id，对照账本找到它所属的票和任务。记为 `blocked` 的任务，在 `state.md` 写清楚在等谁、要做什么、结果交回哪里。

## worker 的授权

用户以本技能调用并指定仓库和范围，即授权 worker 提交自有任务分支、推送、创建或更新 PR，以及在 Issue 和 PR 上发必要的评论。生产操作、正式合并和其他仓库的操作，仍按已有授权处理。

## 恢复

`wait` 和 relay 都是先把消息标为"已送达"，再交给你。所以换会话、上下文压缩或意外中断之后：

1. 按[账本](../../ai-coding/references/ledger.md)回读 `state.md`。
2. 找回上次已经取走的消息：
   - Claude Code：读上一次后台 `wait` 的输出；
   - Codex：用记录的进程号确认旧 relay 的状态，仍在运行但指向旧会话的，停掉它。
3. 把账本与 `<状态目录>/router.db` 的 `message` 表、`relay.log`、`tmux ls` 列出的 `wfc-*` 会话逐一对照。已经送达但账本里还没处理的消息，按上面的消息处理表补做。
4. 重新挂 `wait`，或者用当前会话的 UUID 重新启动唯一的一个 relay。
