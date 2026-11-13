# 09: Windows 原生验收与分发

**What to build:** 从总控派发一个角色任务到结果/提问唤醒，再到异常时可诊断或接替的完整行为；本票只实现标题对应的最小闭环。

**Blocked by:** 08: Herdr 单宿主闭环.

**Status:** ready-for-agent

## Acceptance criteria

- [ ] 原生 Windows 无 WSL 完成完整任务闭环和接替矩阵
- [ ] 安装/启动入口、版本输出和文档与实际一致
- [ ] 重复请求、权限提示、worker 崩溃、router 重启、回收均通过
- [ ] 发布前保留真实日志与限制清单；不把未覆盖宿主写成全部支持

## Testing boundary

- 通过现有 wfc CLI 和受控宿主替身验证外部行为。
- 不把上层工作流、审查策略或发布动作写入核心。
