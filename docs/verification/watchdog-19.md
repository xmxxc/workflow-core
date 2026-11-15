# Issue #19 验证与独立审查

日期：2026-11-14。基线为已集成的 `release/v0.4.6`，commit `cc12a8dee35c3d72bd76e5b5db8fc2cb72a5a968`。隔离工作树起点干净；原开发目录、#17 候选、事故数据库与 relay 未修改。

## 固定候选

SHA-256（审查期间产品及测试内容固定）：

- `router.py`: `dfce789531aba301b7d5e684415ed21a4ca16bd466014fa975155faba20aef96`
- `tests/test_router.py`: `1e468e9cf90b5a5bace53a6e74add449901c2d9edc919483f6ac77ed490cb3f7`
- `docs/watchdog-recovery.md`: `fc118d068b9a1281e996ebd608863110515a66d26269f8dfeb35aaa95937f742`

## 确定性回归

旧实现中，同一 attempt 的 quota 与最大轮次超时同时存在时，六次独立进程的 CLI `wait --timeout 0` 返回：

```text
blocked, unknown, blocked, unknown, blocked, unknown
```

修复后同一测试只返回 `blocked, unknown`，其余检查无新消息。每次调用都新开 router 进程，验证去重不依赖进程缓存。

新增 CLI/受控宿主用例覆盖：六个并发 watchdog、relay 停止后重启、新宿主 turn、同 turn 的错误变化、恢复后新故障、新 round、重试 attempt，以及采样期间收到新输入时丢弃旧采样。并发检查对外只观察到两个不同的通知 ID；relay 对外信封仍为 `unknown`，不提前宣称接收成功。

## 独立审查

以同一冻结的基线到候选 diff 和恢复文档快照，分别运行 Standards 与 Spec 独立审查。两路均未修改候选。增加 relay/采样竞争测试后，双方复核最终快照并再次通过。

- Standards：0 项；遵循标准库、SQLite 和既有 CLI 测试约定。消息与去重记录同属 `BEGIN IMMEDIATE`，owner fencing 保留。
- Spec：0 项；独立故障、持久去重、并发串行化和输入/attempt 边界符合要求；恢复文档保留 ACK 与送达对账的区别。

独立审查属于源码审查；完整回归由实施会话执行。受控 tmux/宿主测试不等于真实宿主验收。本次没有启动或恢复事故范围的 relay。

## 执行结果

- `python3 -m unittest discover -s tests -v`：228 项通过，357.818 秒；产品代码固定为上述 SHA-256。该进程启动后补入两个最终测试，因此另执行最终专项集。
- `PYTHONPATH=.:tests python3 -m unittest test_router.HostErrors -v`：最终 15 项全部通过，44.519 秒，包含 relay 重启和采样期间新输入的两个补充用例。最终候选总计 230 个测试均被执行覆盖。
- `python3 -m py_compile router.py tests/test_router.py`、`git diff --check`：通过。
- `python3 build.py --out /tmp/wfc-19-build` 和构建物 `--version`：通过，版本保持 `0.4.6`；本 PR 不进行版本发布。

验证日志保存在实施环境 `/tmp/wfc-19-full-tests.log` 与 `/tmp/wfc-19-focused-final.log`。本报告保留测试数量、固定源码身份与主要复现输出，远端 CI 结果另见 PR checks。
