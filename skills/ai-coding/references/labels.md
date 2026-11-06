# 标签

## 查找映射

按顺序找项目的实际标签字符串；项目约定优先于本文件的默认名：

1. `docs/agents/issue-labels.md`：triage 与生命周期标签的完整映射；
2. `docs/agents/triage-labels.md`：只有 triage 角色的映射；
3. tracker 里已有的标签：找语义相同的现有标签。

缺少生命周期映射时，在本范围 `prompts/issue-labels-draft.md` 写一份草稿，优先对应 tracker 里语义相同的现有标签，然后问用户一次是否采用。采用前：

- 不切换生命周期标签；把当前阶段写进 Issue 里程碑评论；
- triage 标签照常处理：开工时移除 `ready-for-agent`，收尾时清理残留的 triage 标签。

只在用户确认后创建远端标签或写入项目映射文件。

## 语义

| 映射键 | 默认名 | 含义 |
| --- | --- | --- |
| `triage.needs-triage` | `needs-triage` | 范围尚未确认 |
| `triage.needs-info` | `needs-info` | 等报告者补充信息 |
| `triage.ready-for-agent` | `ready-for-agent` | 范围和验收条件已明确，可以开工 |
| `triage.ready-for-human` | `ready-for-human` | 需要人来实施 |
| `triage.wontfix` | `wontfix` | 不纳入工作范围 |
| `workflow.in-progress` | `in-progress` | worker 已开工 |
| `workflow.in-review` | `in-review` | PR 候选正在接受独立审查 |
| `workflow.changes-requested` | `changes-requested` | 审查或验收要求修改代码 |
| `workflow.ready-for-acceptance` | `awaiting-live` | 已放行，等待合并、部署与真实验收 |
| `workflow.live-verified` | `live-verified` | 真实验收通过 |
| `workflow.blocked` | `blocked` | 缺少权限、能力、决定、依赖或环境 |

每个 Issue 同时只有一个生命周期标签；`live-verified` 是终态，关闭后保留在票上作为记录。

## 切换时机

放行及之前的标签由总控在回读到对应证据后切换，任务文件另有授权时执行者也可以切换；放行之后的标签由 `live-verify` 切换。

| 时机 | 切换 |
| --- | --- |
| worker 开工 | 移除 `ready-for-agent`，加 `in-progress` |
| PR 创建或更新，进入审查 | `in-review` |
| 审查发现阻塞 | `changes-requested` |
| 修复后候选回到审查 | `in-review` |
| 最终完整审查和必要 CI 通过，放行 | `awaiting-live`；合并后、部署前保持不变 |
| 缺外部条件 | `blocked`；解除后回到受阻前的阶段 |
| 真实验收通过 | `live-verified`，按授权关闭 |
| 验收通过，但发现与本票无关的新问题 | 原票 `live-verified`；新票从 `needs-triage` 开始，并回链原票 |
| 验收发现本票需求不成立 | `changes-requested`，原票保持打开；修复后重新放行，回到 `awaiting-live` |
| 验收受环境或平台阻塞 | `blocked`；解除后回到 `awaiting-live` |

每次切换都按 [账本](ledger.md) 的留痕规则入账。
