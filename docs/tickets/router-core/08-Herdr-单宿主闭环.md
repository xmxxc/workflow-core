# 08: Herdr 单宿主闭环

**What to build:** 从总控派发一个角色任务到结果/提问唤醒，再到异常时可诊断或接替的完整行为；本票只实现标题对应的最小闭环。

**Blocked by:** 07: 原生 Windows 核心平台层.

**Status:** ready-for-agent

## Acceptance criteria

- [ ] 在原生 Windows 上通过 Herdr 启动一个受支持宿主
- [ ] 完成派发、提问/唤醒、完成、异常、resume 和 close
- [ ] Herdr handle 与 wfc attempt/ownership 正确对应
- [ ] 输入 uncertain、事件 gap 和后端重启有可诊断结果

## Testing boundary

- 通过现有 wfc CLI 和受控宿主替身验证外部行为。
- 不把上层工作流、审查策略或发布动作写入核心。
