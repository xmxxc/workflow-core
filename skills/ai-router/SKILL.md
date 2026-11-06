---
name: ai-router
description: "多票据开发总控（router 版）：用 workflow-core router 把实施与独立审查派发到 Codex、OpenCode 或 Claude Code 会话，总控睡眠、由 worker 消息唤醒；修复复用原会话并增量复核，放行前最终完整审查。"
disable-model-invocation: true
---

# 开发总控（router 版）

你是本范围唯一的总控。范围、依赖、候选、证据、留痕、标签和放行都按原型 [ai-coding](../ai-coding/SKILL.md) 的规则执行，包括它的"按场景读取"、完成标准和全部 reference。本技能只替换派发方式：所有实施者和审查者都通过 workflow-core 的 router 派发，你在它们工作时睡眠，由它们的消息唤醒。

用户手动调用并指定仓库和范围，授权范围与 ai-coding 相同，worker 的授权见 [router 派发](references/router.md)。正式合并、部署和真实验收另行授权，交给 `live-verify`。

## 你在哪个宿主

- **Claude Code**：每次派发或回复之后，后台挂一个 `router.py wait`，然后结束当前回复。
- **Codex**：启动一次 `router.py relay --detach`，消息会作为新的一轮送到你这里。总控会话用 `danger-full-access` 启动。

两种做法的具体命令和恢复步骤，都在 [router 派发](references/router.md)。

## 审查节奏

每张票按下面的顺序推进：

1. **首次完整审查**：实施者第一次报告 `done` 之后，派发一个独立审查者，审查从 PR 基线到候选的全部净变化。
2. **修复与增量复核**：审查有阻塞时，用 `continue` 把修复任务交给原来的实施者，让它在原会话里接着做。修复完成后，同样用 `continue` 让原来的审查者复核本轮补丁（上次获审候选 → 新 head）以及它影响到的调用和契约。原会话保留着上下文，所以修复和复核都只针对这次的补丁。补丁影响不清、做过 rebase 或 force-push 时，扩大为完整审查。
3. **最终完整审查**：放行之前，对 PR 基线到最终候选做一次完整审查。这一步可以继续用原审查者，也可以另派一个新的审查者。

三种审查的定义、独立性要求和记录格式，以 [ai-coding 的独立审查](../ai-coding/references/review.md) 为准。

## 完成标准

与 ai-coding 相同，另外加两条：

- 每个 dispatch 的最终状态都记入账本：`returned`、`stale` 或 `blocked`，并写明原因。
- 范围完成时，停掉 relay，列出 worktree 和 router 状态目录作为回收清单。
