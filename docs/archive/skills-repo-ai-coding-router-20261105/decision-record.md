# ai-coding 新增 router 派发模式（2026-11-05）

起点为 `9d38b84`。`ai-coding` 新增第四种派发模式 `dispatch/router.md`。总控在 Claude Code 或 Codex 中运行，用 workflow-core 的 `router.py` 把实施者和审查者派到各宿主的 tmux 会话中，然后后台等待它们的消息唤醒。原有三种模式、共用 reference 和三个旧名别名都不变。

## 原因

- 用户要求补齐工作流的最后一块：总控能够睡眠，worker 或审查者在独立窗口中运行完毕后唤醒总控。原有的宿主独立任务模式需要总控一直保持唤醒来等待回收。
- 2026-11-05 在 `/path/to/workflow-core` 实测了 Claude Code、Codex、OpenCode 和 Orca（记录见该仓库 `docs/probes-20261105-*.md`）：
  - 三家宿主的完成钩子都会把"worker 停下来问人"当成"完成"，所以不能靠钩子判断完工；
  - Orca 的编排可用，但 worker 失败时不会通知总控，而且依赖桌面应用。
- 用户因此选择自研一个轻量 router：worker 主动用 `done` 报告完工、用 `ask` 提问；总控用后台 `wait` 等待；看门狗把会话消失报为 `exited`，把画面长时间不变报为 `stalled`。router 在 `/path/to/workflow-core` 的 `router` 分支，提交 `15de395`、`c9bce1f`、`186d1b9`。
- 用户确认的模型与宿主绑定：GPT 用 Codex，Claude 用 Claude Code，其他模型用 OpenCode。三家都用各自的官方 CLI 和登录方式，router 支持全部三种 worker。

## 决定

1. 新增 `dispatch/router.md`。每个任务的回报放在独立子目录 `reports/<任务名>/`，这样 worker 只能写自己的回报。派发成功只记 `pending-handoff`，看到 worker 已进入任务或收到它的消息才记 `accepted`。文件写明以下内容：
   - 前提：router 路径、每个范围一个状态目录、总控与 worker 使用同一个 tmux 服务、worker 工作目录已被宿主信任；
   - 派发命令，以及 `accepted` 和 `returned` 各自对应的 router 证据；
   - 后台 `wait` 与唤醒的节奏；
   - `done`、`question`、`stalled`、`exited` 四类消息分别怎么处理；
   - worker 授权与恢复。
2. `SKILL.md` 的派发模式列表和 description 加入 router 派发，`agents/openai.yaml` 同步修改说明文字。调用策略不变，仍然只能手动调用。
3. 独立性继续沿用 `references/review.md` 的定义。实施和审查分别派发，dispatch id 不同即为不同会话，以此作为独立性证据；文件隔离不作为证据。
4. 首次真实运行后补充两项（依据见下文"补充"）：OpenCode worker 的外部路径一律放行；新增"Codex 总控"，由 relay 唤醒。

## 限制与待办

- **Codex 联网**：Codex worker 在 `workspace-write` 沙箱下默认不能联网，推送和建 PR 可能被拦截。router 实测只覆盖了本地写回报，没有覆盖推送。模式文件要求先确认 Codex 能否联网，或者改派其他宿主。
- **新目录信任**：Codex 的信任作用到仓库根，用户确认一次就覆盖该仓库的所有 worktree。Claude Code 对新路径是否同样处理，还没有在 worktree 上验证。
- **提醒 worker**：提醒停住的 worker 时，目前由总控直接用 `tmux send-keys` 打字，router 还没有对应命令。
- **审查者能读取 router 状态**：每个 worker 只能写自己的回报子目录，但同一范围的 router 状态库对所有 worker 可读，其中包括实施者的提问内容。把它当作已知的独立性限制；需要更严格隔离时，审查改用其他派发模式。
- **恢复时可能漏消息**：`wait` 先标记已送达、再输出。恢复时要先读上一次 `wait` 的输出，再对照 `router.db` 和 `wfc-*` 会话，模式文件已写明这一步。
- **未在真实项目中运行**：`ai-coding` 以本模式完整推进一张真实票据的回放尚未执行。router 本身已经用真实 worker 跑通了派发、提问、回答、完工，以及 `stalled` 和 `exited` 两种情况。

## 补充：首次真实运行之后（2026-11-05）

在 xmxxc/example-project #2 上用本模式实际推进（Claude Code 总控，OpenCode 实施，Codex 审查）。

观察到的事实：

- OpenCode 访问任务文件、工作区根、`~/.config/git`、`/etc`、`/tmp/opencode` 时，都停在外部目录确认提示上，看门狗把每一次都报成了 `stalled`。
- 一次实测里，OpenCode 对某个目录有一条禁止编辑的规则，但 worker 用 shell 命令仍然在该目录写入了文件。所以它的路径规则只作用于工具层面，并不是沙箱。
- Codex 的信任提示写明：在 git 子目录或 worktree 中确认的信任，作用到仓库根。
- 总控尝试替用户确认信任，被 Claude Code 的权限系统以"削弱安全设置"为由拒绝。
- Codex 审查者在 `workspace-write` 沙箱下不能联网，因此装不了依赖，也发不了 PR 评论。
- relay 用真实 Codex 会话验证通过：OpenCode worker 的 `done` 经 `codex queue --resume` 投递后，Codex 开始了新的一轮。

据此做出的决定：

- 用户决定：OpenCode worker 的外部路径一律放行，router 已按此修改。
- 信任由用户在仓库根确认一次，总控不代为确认。
- Codex 审查者断网时，由总控在固定快照上执行全量检查并保存日志，同时提供需求原文；审查结论标为待发布，由总控转录到 PR。
- 用户决定：Codex 总控会话以 `danger-full-access` 启动，用 relay 唤醒；`wait` 和 relay 不能同时运行。

## 验证

以下检查实际执行并通过：

- `validate_skill.py`：42/42 通过；
- 不带上游参数的 `validate_base.py`：退出码 0，`errors` 为空；
- `check_review_scope.py`；
- `git diff --check`。

`check_upstream_validation.py` 以退出码 1 结束，因为本机没有固定的 Firecrawl 检出（`/path/to/firecrawl-skills`）。本次没有改动任何上游来源文件，2026-10-27 的合并记录也因同样原因没有运行它。

`ai-coding` 以本模式进行的行为回放没有执行，见上文"限制与待办"。
