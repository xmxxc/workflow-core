# 原生 Windows 核心平台层

本层为 Herdr 调度提供可导入的 Python 核心、SQLite 状态与原生平台边界。宿主闭环在 Herdr 接入后验证；不重复验收既有 tmux 在 Windows 上的限制。

通过标准 Windows Python 3.9+ 运行：

```powershell
python wfc.py --help
python wfc.py --version
python wfc.py status
python build.py --out dist/wfc.pyz
python dist/wfc.pyz status
```

PowerShell 与 cmd 均可调用 Python；zipapp 无需 Unix executable bit 或 shebang。`status` 不创建状态目录、数据库、锁或进程；旧数据库仍以只读兼容视图读取，不自动迁移。`WFC_STATE` 优先，Windows 默认状态目录为 `%LOCALAPPDATA%\workflow-core`；未设置 LOCALAPPDATA 时使用用户目录下 `AppData\Local\workflow-core`。显式 UNC 路径交给 Windows/SQLite 文件系统处理；不承诺网络共享提供与本地磁盘相同的锁与事务保证。受限目录的写入错误不会降级为另一状态目录。

锁在 Windows 使用 `msvcrt.LK_NBLCK` 对文件首字节加锁，Unix 使用非阻塞 flock。双方均以单调时钟等待，默认最多 30 秒；关闭文件描述符或进程退出释放锁。输入锁始终在数据库事务前获取，超时以 `input_lock_timeout` 拒绝输入，不无锁执行。宿主信任配置的锁失败仅跳过自动预信任，交由宿主确认。

Windows waiter 探活使用 `OpenProcess` 的只读查询/同步权限、`WaitForSingleObject` 与 `GetProcessTimes` 的 creation FILETIME。同一 handle 获取活性和创建身份，避免两次 PID 查询跨复用。权限拒绝为 `unknown`，在 status 中保留并显示 diagnostic；不会把不可查询视作退出，也不调用 `os.kill(pid, 0)`。已知 creation identity 不匹配的 waiter 不显示为原进程。旧无 identity 的记录只能提供 PID 活性，不能作为关闭进程的授权。

`relay --detach` 在 Windows 使用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`，Unix 保留 setsid。日志写入状态目录，子进程不继承终端 stdio；启动父进程退出后可继续运行。Windows Job Object/组织终端策略仍可能约束子进程生命周期，不能承诺逃逸管理策略。停止前应核对 PID、creation identity、scope 与 state；没有自动终止未知进程的回退。

Windows worker 回调明确为 PowerShell 命令，使用 `Start-Process` 的单个 CRT 转义参数串，并以 PowerShell 单引号 literal 包裹；保留内嵌双引号和空参数，兼容 Windows PowerShell 5.1。owner token 从继承环境读取。路由执行仍以 argv 调用，不把宿主参数拼成 shell 程序。CLI JSON、router 文本文件与 Windows stdio 使用 UTF-8。Windows 不自动改写 Claude 信任配置，因为 Unix mode 不能证明 ACL 安全；由用户在宿主确认。

## 验证范围

跨平台合同测试使用 Python 子进程，不依赖 chmod、Unix shebang 或伪 tmux 可执行脚本。覆盖只读 CLI、Unicode/空格路径、有界锁竞争、崩溃释放、并发写串行化、回调 argv 和父进程退出后的 detached 子进程。原生 Windows CI 另执行 creation identity/PID 不匹配测试和既有非终端 backend 合同；Unix 原 suite 继续执行。

核心合同通过不等于真实 Codex/Herdr 闭环通过。按用户确认，接入 Herdr 后再做宿主调度验证；平台/宿主支持声明以该阶段的实际结果为准。

实现依据：[Python msvcrt locking](https://docs.python.org/3/library/msvcrt.html#msvcrt.locking)、[Microsoft GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes)、[Python detached process flags](https://docs.python.org/3/library/subprocess.html#subprocess.DETACHED_PROCESS)。
