# router 派发

你用 workflow-core 的 `router.py` 把 worker 和审查者派发到各自宿主的 tmux 会话中，然后睡眠，由 worker 的消息唤醒你。总控在 Claude Code 中时，在后台挂 `wait`；在 Codex 中时，见文末的 [Codex 总控](#codex-总控)。worker 可以是 Codex、OpenCode 或 Claude Code。

## 前提

- **router 位置**：先用用户本次给出的 `router.py` 路径；没有就用账本已记录的路径；两处都没有时，问用户一次。
- **状态目录**：每个范围用一个独立的 `WFC_STATE`，例如 `~/.local/state/workflow-core/<仓库>-<范围>`。对该范围执行的每条 router 命令都要带上它；路径写入 `state.md`。
- **同一 tmux 服务**：每个 worker 运行在名为 `wfc-<dispatch id>` 的 tmux 会话中。总控、worker 和用户执行 `tmux attach` 时，都要连接同一个 tmux 服务（同一个用户，同一个 `TMUX_TMPDIR`）。看门狗找不到会话时，会把它报为 `exited`。
- **信任**：worker 的工作目录需要先被它的宿主信任，否则会停在信任提示上，随后被看门狗报为 `stalled`。Codex 的信任作用到 git 仓库根：接管时请用户在仓库根运行一次 `codex` 并选择信任，之后这个仓库的所有 worktree 都不会再问。信任由用户亲自确认。

## 派发

每个任务执行一次：

```sh
WFC_STATE=<状态目录> python3 <router.py> dispatch --host <codex|opencode|claude> [--model <模型>] \
  --task <prompts/ 中任务文件的绝对路径> --cwd <工作树> --report <reports/<任务名>/ 下的回报绝对路径>
```

- 宿主和模型按用户的角色分配来选。派给 OpenCode 时必须带 `--model`，因为它的默认模型可能是受限的免费模型。
- **回报**：每个任务的回报放在自己专属的子目录里，例如 `reports/113-impl-r1/report.md`，任务文件里"回报"一行写同一个路径。router 会建好这个目录并开放给该 worker 写入。
- worker 在无人值守下运行，权限由各自宿主限制：
  - Codex：`workspace-write` 沙箱，不请求审批；
  - OpenCode：工作目录以外的路径一律放行，没有目录限制。它的路径规则只作用于工具，挡不住 shell 写入。
  - Claude Code：`auto` 模式。

  worker 用 `done` 报告完工，用 `ask` 提问。这两条命令的用法由 router 写进 worker 的说明，任务文件不用重复。
- **Codex 沙箱默认不能联网**。任务需要推送分支、创建 PR 或发评论时，先确认 Codex 当前配置能联网；不能联网就改派 Claude Code 或 OpenCode，或者由你按授权代为发布。
- **给断网的审查者准备材料**：审查者是 Codex 时，派发前先做两件事：
  - 在固定快照上执行项目的全量检查（安装、类型、lint、格式、测试），日志保存到它的回报目录，记录每条命令真实的退出码；
  - 把需求来源（Issue / spec 正文）写进同一目录。

  审查任务文件指向这两份材料，审查结论写进报告并标为"待发布"，由你注明原作者后转录到 PR。这也就是[验证分层](../references/verification.md)里由一个明确步骤负责的共用验证。

每个派发返回一个 dispatch id，把它记为该任务的标识。各状态需要的 router 证据如下：

| 状态 | router 证据 |
| --- | --- |
| `pending-handoff` | `dispatch` 返回了 dispatch id：会话已建立，但还没有证据表明 worker 已收到任务 |
| `accepted` | 收到该 dispatch 的任一条消息，或者 `tmux capture-pane -p -t wfc-<id>` 显示 worker 已开始处理任务 |
| `returned` | 收到该 dispatch 的 `done`，并按[账本](../references/ledger.md)完成 `returned` 的回读核对 |

**独立性**：实施和审查分别派发。不同的 dispatch id 对应不同的宿主会话和新的上下文。各 worker 都能读到同一个状态库，OpenCode worker 还能写任意路径，所以文件隔离不作为独立性证据。审查者的 `--cwd` 用固定快照或独立工作树，见[独立审查](../references/review.md)。把双方的 dispatch id 记为独立性证据。

## 等待与唤醒

派发后，在后台挂上等待：

```sh
WFC_STATE=<状态目录> python3 <router.py> wait --timeout <秒> [--stall-after <秒>]
```

挂好后结束当前回复，等待被唤醒。`wait` 一返回你就会醒来：先处理输出里的全部消息并写入账本，再挂下一次 `wait`，然后结束当前回复。同一时刻只保留一个 `wait`。用户在窗口里发言也会唤醒你；回应完用户后，确认后台的 `wait` 还在运行。

`wait` 返回退出码 3 表示超时，期间没有收到任何消息。这时回读各任务的现场：回报、分支，以及 `tmux capture-pane -p -t wfc-<id>` 的画面。有进展就重新挂 `wait`；没有进展的任务记为 `blocked`。

## 消息处理

| 消息 | 处理 |
| --- | --- |
| `done` | 按"派发"一节的 `returned` 证据完成核对。缺留痕时，按[账本](../references/ledger.md)补发转录，或者交回 worker 补发 |
| `question`（worker 用 `ask` 发出） | 已固定的范围、验收条件和授权能回答的，直接执行 `router.py reply <消息 id> "<答案>"`。需要用户决定的，把原问题和选项原样转给用户，拿到答复后再 `reply`。问题和答案都记入账本 |
| `stalled` | 读附带的画面。<br>停在信任或权限提示上：请用户执行 `tmux attach -t wfc-<id>` 处理，该任务记为 `blocked`。<br>停下了却没报告也没提问：回读现场，然后用 `tmux send-keys -t wfc-<id> -l "<提醒>"` 打一行提醒，隔半秒再发 `Enter`，提醒它按说明用 `done` 或 `ask`。这次介入要记入账本。提醒后再次报为 `stalled` 的，记为 `blocked` |
| `exited` | 先检查 worker 退出前是否已经提交或推送，然后按[账本](../references/ledger.md)的失效规则处理：记为 `stale`，保留现场，用下一轮编号改派，在新任务文件里写明真实起点，以及旧现场是续用还是放弃 |

每条消息都带 dispatch id，对照账本找到它所属的票和任务。记为 `blocked` 的任务，在 `state.md` 写明在等谁、要做什么、结果交回哪里。

## worker 的授权

用户以本模式调用并指定仓库和范围，即授权 worker 提交自有任务分支、推送、创建或更新 PR，以及发必要的 Issue/PR 评论。生产操作、正式合并和其他仓库的操作，仍按已有授权处理。

## 恢复

`wait` 取走消息时会先把它标为已送达，再输出。所以换会话、上下文压缩或意外中断之后，单靠重新执行 `wait`，拿不回已经取走却还没处理的消息。恢复按以下顺序进行：

1. 按[账本](../references/ledger.md)回读 `state.md`。
2. 读上一次后台 `wait` 的输出（宿主的后台任务输出文件）。
3. 把账本与 `<状态目录>/router.db` 的 `message` 表、`tmux ls` 列出的 `wfc-*` 会话逐一对照。尤其要找出那些已经提问、但账本里还没有答复的任务，按 `question` 处理。
4. 对账完成后，再挂 `wait`。

## Codex 总控

总控在 Codex 中时，按以下方式运行：

- **启动**：用 `danger-full-access` 启动总控所在的这一个 Codex 会话，因为它需要用 `gh` 访问 tracker 和 PR，还要运行 router。worker 和审查者仍按"派发"一节的限制运行。
- **唤醒**：不挂 `wait`。接管后，用你的会话 UUID 在后台启动一次 relay，把它的进程号记入 `state.md`：

  ```sh
  WFC_STATE=<状态目录> nohup python3 <router.py> relay --codex-thread <你的会话 UUID> [--stall-after <秒>] >> <状态目录>/relay.log 2>&1 &
  ```

  会话 UUID 的找法：在 `~/.codex/sessions/` 下找到包含本会话启动提示原文的 rollout 文件，取它第一行的 `id`。
- **节奏**：每次派发或回复后，结束当前回复。relay 会把 worker 的消息作为一条以 `[router] Messages for the controller:` 开头的新消息投递给你；处理完它的全部消息，再结束当前回复。
- **恢复**：`wait` 和 `relay` 同一时刻只能运行一个，否则两者会抢同一批消息，被 `wait` 取走的消息你永远看不到。relay 投递失败时会把消息放回，下一轮重新投递。换会话、上下文压缩或意外中断之后，按以下顺序恢复：
  1. 按[账本](../references/ledger.md)回读 `state.md`。
  2. 用 `state.md` 里记录的进程号确认旧 relay 的状态。仍在运行但指向旧会话的，停掉它。
  3. 把账本与 `<状态目录>/router.db` 的 `message` 表、`relay.log`、`tmux ls` 列出的 `wfc-*` 会话逐一对照。已投递给旧会话但账本还没处理的消息，按消息处理表补做。
  4. 用当前会话的 UUID 重新启动唯一一个 relay，并记下新的进程号。
