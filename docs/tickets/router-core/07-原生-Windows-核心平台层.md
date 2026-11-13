# 07: 原生 Windows 核心平台层

**What to build:** 从总控派发一个角色任务到结果/提问唤醒，再到异常时可诊断或接替的完整行为；本票只实现标题对应的最小闭环。

**Blocked by:** 06: 执行后端接口.

**Status:** ready-for-agent

## Acceptance criteria

- [ ] Windows Python 可运行 help、version、只读 status 和状态库操作
- [ ] 文件锁、进程 creation identity、后台生命周期、路径/argv/UTF-8 有平台实现
- [ ] Unix 既有测试继续通过
- [ ] Windows 受控替身覆盖空格、Unicode、盘符、UNC、权限和进程复用

## Testing boundary

- 通过现有 wfc CLI 和受控宿主替身验证外部行为。
- 不把上层工作流、审查策略或发布动作写入核心。
