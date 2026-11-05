# 交接：ai-coding 总控"完工唤醒"（第一阶段）

日期：2026-11-04 · 来源会话工作目录：`/path/to/skills-repo` · 本会话未修改任何仓库文件

## 目标

用户最初要解决的问题：**总控窗口可以睡眠；worker 或审计在各自的独立窗口里跑完后，自动唤醒总控。** 不再需要总控一直醒着轮询。总控、worker、审计都是交互式窗口，不用无头模式。

## 现有工作流（只引用，不复述）

- 总控：`skills/engineering/ai-coding/SKILL.md`，以及 `dispatch/{manual,host,codex}.md` 和 `references/{ledger,handoff,review,verification,labels,release}.md`
- 验收：`skills/engineering/live-verify/SKILL.md`
- 设计记录：`docs/ai-coding-unification-20261027.md`、`docs/generic-controller-review-design-20261024.md`、`docs/issue-controller-live-verify-design-20261024.md`
- 仓库规则：`AGENTS.md`（改技能前要读的文档、验证脚本、提交前审查、推送需要单独授权）

## 已经定下的

1. **模型绑定自家宿主**：GPT 用 Codex CLI（ChatGPT 订阅），Claude 用 Claude Code（Claude 订阅），其他模型（例如 DeepSeek）统一走 OpenCode（API key）。三条都是官方渠道，没有订阅合规问题。
2. **角色不固定**：哪个宿主当总控、worker、审计都可以切换，但每个宿主只跑它自家的模型。
3. **五个阶段**，每一阶段都在前一阶段上增加，单独可用：
   1. **完工唤醒**（本交接的重点）
   2. 多宿主分角色
   3. 独立运行：用户只往 Issue 里放 `ready-for-agent` 的票，总控主动扫描，**先走 observe**（列出打算接的票，用户确认后才接）
   4. 总控减负：机械性的工作（标签、留痕检查、候选身份）交给程序
   5. 花式唤醒：Hermes 网关、MCP、面板、多种事件源
4. `live-verify` 保持人工交接。

## 第一阶段的方案

```
worker / 审计窗口跑完
  → 该宿主自己的完成钩子
  → notify-controller 小脚本（按总控所在宿主选择投递方式）
  → 总控窗口醒来，读回报，按账本继续推进
```

不需要调度器，也不需要无头总控。`ai-coding` 的账本和派发逻辑基本不变，只是把"等待回收"从"总控一直醒着"改成"睡下去，被叫醒"。很可能需要新增一个派发模式，或者修改 `dispatch/host.md`。

| 宿主 | 完成钩子（worker 一侧） | 唤醒入口（总控一侧） |
| --- | --- | --- |
| Claude Code（本机 2.1.318） | `Stop` hook | 总控挂一个后台等待命令，事件到了命令退出，窗口随之被唤醒；或者用本机会话间的 SendMessage（待实测） |
| Codex CLI（本机 0.166.0） | hooks（`codex features list` 显示 `hooks` 为 stable） | `codex queue --thread <总控会话> --message … --resume` |
| OpenCode（npm 最新 1.19.5，本机未安装） | 插件事件 `session.idle` | `POST /session/:id/prompt_async`（服务端 HTTP 接口） |

## 第一阶段要实测的事项（先做这些，再写脚本）

1. **Claude Code**：总控窗口空闲时，后台等待命令退出能否稳定唤醒它？用户正在输入时又会怎样？总控每轮结束前是否都要重新挂上等待命令（需要写进技能说明）？
2. **Claude Code**：两个 Claude Code 窗口之间，SendMessage 能否唤醒对方？如果能，Claude Code 当 worker 时可以不用小脚本。
3. **完成钩子误触发**：`Stop` hook（以及 Codex hooks、OpenCode 的 `session.idle`）会不会在 worker 中途等人回答时就触发？应该用"回报文件已写入"作为唤醒条件，而不是"会话停了"。
4. **Codex**：`codex queue --resume` 投递给一个开着的 Codex TUI，它是立即处理，还是要等用户下次输入？
5. **OpenCode**：TUI 模式下内置的服务端端口能否被脚本调用 `prompt_async`（文档说启动 TUI 时会同时起一个服务端，可以用 `--port` 固定端口）。

实测完成后再确定 notify-controller 的接口，以及 `ai-coding` 需要改哪些文件。

## 调研过、已经放弃的方向（背景参考）

- **dsh-superagent / DeepSeek Harness**：理念借鉴自这里（"总控只路由、不等待"、`observe` → `auto`、按"在等谁"划分阶段、一次只问一个具体问题），图解见 https://claude.ai/artifact/AKnFQHxbujnscn7yMQjwBp。DSH 原生支持唤醒，但 Claude 只能用 API key；它的订阅登录来自 pi-ai，借用的是官方客户端身份（Anthropic 用的是 Claude Code 的客户端 ID，被明确禁止；Codex 的属于灰色地带）。放弃的原因：用户要用订阅，并且要用交互式窗口。
- **pi（earendil-works/pi）**：极简宿主，扩展可以 `triggerTurn` 唤醒会话；订阅登录的问题同上。
- **vibe-kanban**：可以调度 Claude Code、Codex、OpenCode，自带 MCP，但**正在停止维护**（README 写着 "sunsetting"，最后正式版 v0.1.44，2026-04）。它的通知只有系统弹窗和提示音，没有唤醒 agent 的能力。
- **自己写常驻调度器加事件库加游标**：留给第三到第五阶段。设计要点：事件带幂等键；游标分两种，来源游标和消费游标（决定写入账本后才 ack）；事件和游标存在 checkout 之外，`state.md` 仍留在 `docs/agents/work/`。
- **订阅合规的依据**：Anthropic 自 2026-02 起，订阅 OAuth 只限 Claude Code 和 Claude.ai 使用（https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access）；OpenCode 官方文档写明 Claude Pro/Max 插件 "Anthropic explicitly prohibits this"，从 1.3.0 起已移除。
- **自动化使用订阅的风险**：高频调用会撞上订阅额度。风险低，但不是零。

## 还没定的事

- `ai-coding` 是新增第四种派发模式，还是修改 `dispatch/host.md`。
- 第一阶段的总控用哪个宿主（倾向 Claude Code，用户偏好 Claude）。
- 第二阶段以后再讨论的：通知方式；`state.md` 是否保留；审查由代码触发还是由总控触发。

## 建议使用的技能

- `diagnosing-bugs`：实测时唤醒不生效、钩子误触发等问题的排查。
- `update-config`：配置 Claude Code 的 `Stop` hook 等 settings.json 项。
- `writing-for-agents`：修改 `ai-coding` 的 `SKILL.md` 或 dispatch 文件时使用；改之前先按 `AGENTS.md` 读 `docs/workflow-principles.md` 和 `docs/skill-selection.md`。
- `implement` 配合 `ponytail`：编写 notify-controller 小脚本，保持最小实现。
- `research`：需要继续查 Codex hooks 或 OpenCode 插件文档时使用，结果写进仓库的 Markdown 文件。
- `grilling`：在把第一阶段的方案写进设计文档之前，先压力测试。
