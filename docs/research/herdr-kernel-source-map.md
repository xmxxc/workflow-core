# Herdr 0.9.5 执行内核裁剪：源码与依赖边界

调查日期：2026-11-15。固定来源为 `herdrdev/herdr` 的 `ee5bfb5041bb3e4a49d591c72308aae3c81ecdde`，本地只读副本 `/tmp/wfc-herdr-source`。本文是源码调查与原型设计依据，不代表 Windows 运行验收，也不改变 PR #25 的候选状态。未向 Herdr 上游提交、推送或发布。

## 结论与候选切口

可行的第一个切口是**新的薄执行进程，复用 Herdr 固定的 patched portable-pty 与 Ghostty 绑定**，而非把 Herdr 主程序关掉 UI 后当作小内核。Herdr 已把 PTY I/O actor 与终端绑定分开，但 `PaneRuntime` 本身仍直接依赖 `ratatui`、`AppEvent`、`layout::PaneId`、检测器及渲染信号；复制它会把目标外结构带进来。[Pane imports](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pane.rs#L1-L49)、[runtime ownership](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pane.rs#L1497-L1530)。

这是源码上的可行性判断。薄层是否足够正确、产物多小，必须由隔离构建与行为验证给出；不能由原文件行数或 Linux 构建推出跨平台完成。

建议最小 API：`start(argv, cwd, env, rows, cols)`、`inspect(handle)`、`read(handle, source, max_lines)`、`write/keys(handle, request_id)`、`resize(handle)`、`interrupt(handle)`、`close(handle)`、`hello()`。执行句柄至少绑定服务代次与实例随机身份；操作结果区分明确拒绝、确认接受、未知，不因超时重放输入。精确 API 为待验证设计，不是 Herdr 已有接口的兼容承诺。

WFC 继续拥有 dispatch/round/attempt、消息确认、owner fencing、总控唤醒；内核只证明所拥有的进程/终端事实。恢复先区分客户端重连、服务重新启动、存活 PTY 热移交、重新启动宿主会话四种行为，不能共用“恢复成功”一词。

## 必要模块与依赖闭包

| 能力 | 固定源码依据 | 裁剪方式和不能省略的行为 |
|---|---|---|
| PTY 建立、子进程与 wait | [Windows spawn backend](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pty/backend.rs#L7-L40)、[Unix backend](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pty/backend/unix.rs#L1-L43) | patched portable-pty；Unix 必须正确复制 master FD、CLOEXEC、关闭多余 slave/master；Windows 必须保留 ConPTY 生命周期。直接调用 crate 可先绕开 Herdr App。 |
| 连续读取、输入串行化、终端回复、resize、关闭 | [Unix actor](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pty/actor/unix.rs#L1-L110)、[Windows actor](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pty/actor.rs#L1-L94)、[FD helpers](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pty/fd.rs#L1-L110) | 现有 actor 带 `bytes`、Tokio、tracing、平台输入 guard。可先实现更小的线程/队列，但必须保留读写并行、背压、输入原子序列与终端回复排序，不把队列接收等同宿主执行。 |
| VT 解析与屏幕读取 | [Terminal::new](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/src/lib.rs#L859-L895)、[write/resize](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/src/lib.rs#L973-L1063)、[screen/viewport formatter](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/src/lib.rs#L1561-L1635) | 保留 Ghostty C ABI、Rust bindings、终端状态；滚动、清屏、宽字、alternate screen、bracketed paste 等不能由累计 stdout 替代。`ESC` 是状态输入，不是可简单删去的装饰。 |
| 终端主动回复与按键协议 | [write-PTY callback](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/src/lib.rs#L1144-L1157)、[key encoder](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/src/lib.rs#L3098-L3140) | 解析器产生的查询回复需要回送 PTY；Enter/Ctrl-C 必须按终端输入语义验证，不能将所有键固定替换成文本。鼠标/图像/链接激活可以不作为产品 API，但保留绑定时仍可能链接相关实现。 |
| 退出、关闭和后代进程 | [runtime Drop/shutdown](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pane.rs#L1702-L1809)、[Windows descendants/signals](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L2757-L2807) | wait 与 EOF 分开；树关闭不同于 kill 根 PID。Unix 会话/进程组与 Windows 后代快照策略不同，需各自验证孙进程、父先退、关闭并发。 |
| 进程身份与宿主隔离 | [Windows start token](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L1938-L1961)、[launch environment](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pane.rs#L156-L217) | PID 不是持久身份；保留创建时间/服务代次校验及明确拥有关系。新 worker 默认清除继承的 `CODEX_THREAD_ID` 等旧宿主会话变量，显式环境配置才能恢复。WFC attempt 身份不下放为内核调度职责。 |
| 最小本地通信 | [IPC transport, socket identity](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/ipc.rs#L1-L131)、[Windows private pipe](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L27-L71)、[API listener ownership](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/api/server.rs#L35-L93) | 保留同用户权限、端点独占、清理仅删自身端点、请求边界/大小限制。可以设计独立小协议；无需背负 Herdr App request enum、TUI codec 与远程 attach 兼容面。stdio 原型只验证嵌入进程，不证明 detached daemon/reconnect。 |

直接依赖闭包的起点：Ghostty Rust crate 只有 `base64/libc/png/tracing` 四项直接依赖，portable-pty 还有 `anyhow/downcast-rs/filedescriptor/libc/log/nix/serial2/shell-words` 及各平台依赖；Windows 本地补丁增加哈希验证依赖。完整解析出的 transitive Cargo 闭包应由原型自己的 `Cargo.lock` 与 `cargo tree --locked` 留档，而不是沿用 Herdr 根 Cargo.lock 中所有 TUI/网络依赖。[Ghostty crate manifest](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/Cargo.toml#L1-L14)、[portable-pty manifest](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/portable-pty/Cargo.toml#L1-L121)。

## portable-pty 0.9.0 的实际差异

已下载官方 [0.9.0 crate](https://static.crates.io/crates/portable-pty/portable-pty-0.9.0.crate)，SHA-256 `b4a596a2b3d2752d94f51fac2d4a96737b8705dddd311a32b9af47211f08671e`，在 `/tmp/wfc-portable-pty-upstream` 展开并对比。除 Herdr 未保留 registry 包的 `.cargo_vcs_info.json` 和 `Cargo.lock` 外，只有 `Cargo.toml`、`Cargo.toml.orig`、`src/cmdbuilder.rs`、`src/win/psuedocon.rs` 不同，符合以下四项 patch 记录。此比较是文件级差异检查，不等于所有 Windows 行为已实测。[完整索引与移除条件](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/portable-pty.patches.md#L1-L171)。

| 补丁 | 原因及保留建议 |
|---|---|
| `0001-control-conpty-loading.patch` | 禁止从裸 `conpty.dll` 搜索路径加载其他应用 DLL；相邻 bundle 要精确校验 DLL、x64/ARM64 OpenConsole 哈希，拒绝 reparse point/额外文件，以绝对路径及受限 DLL 搜索加载。无 bundle 使用已加载 kernel32；明确 `HERDR_WINDOWS_CONPTY=system` 可回退。不能为了缩小体积悄悄恢复任意 PATH DLL 加载。 |
| `0002-windows-raw-command-tail.patch` | `cmd.exe /d /c` 原始命令尾与 argv 引用语义不同。如果内核仅支持直接 argv 入口，原型可不用 raw tail，但不能宣称兼容 Herdr 的 shell command 输入。 |
| `0003-reject-malformed-windows-environments.patch` | 排除注册表非字符串/含 NUL/非法环境项，避免 CreateProcessW 错误 87。继续保留。 |
| `0004-skip-directories-when-resolving-windows-programs.patch` | PATH 中同名目录不是可执行文件，例如 PowerShell 目录；继续保留。 |

四个 patch 原文均位于 [vendor/patches/portable-pty](https://github.com/herdrdev/herdr/tree/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/patches/portable-pty)，原型来源清单需记录这些文件，不可只标“上游 portable-pty 0.9.0”。

## Ghostty 构建与供应链

Herdr 固定的 libghostty-vt 上游基点是 `44f2a44df7e8c4a0c6df3f7d872ef3d7ead88e51`，dist archive `libghostty-vt-1.3.2-HEAD-+44f2a44df.tar.gz`；这不是纯净 upstream tree，而是叠加本地补丁。[vendor provenance](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/libghostty-vt.vendor.json#L1-L5)。

Rust 构建脚本从 crate 相对路径 `../../vendor/libghostty-vt` 调用 Zig `build -Demit-lib-vt -Doptimize=ReleaseFast -Dsimd=true -Dtarget=… -Demit-xcframework=false`，归档输出进各 target 的 OUT_DIR。直接把 Rust crate 拷到新层级会破坏此相对路径；隔离原型需保持目录结构或明确记录 build.rs 改动。支持平台映射包含 Linux GNU/musl、macOS、iOS、Windows MSVC；这只是构建映射，不是运行验收。[build.rs](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/build.rs#L1-L137)。

工具链：上游 Rust 固定 `1.96.1`；Ghostty Zig 最低版本与 Herdr错误提示均为 `0.16.0`。Windows MSVC 跨编译要真实 SDK/CRT headers/libs，`LIBGHOSTTY_VT_WINDOWS_LIBC` 传入 Zig libc 配置；这不能用 GNU ABI 替代来声称同一路径通过。[Rust toolchain](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/rust-toolchain.toml#L1-L3)、[Zig dependency manifest](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/libghostty-vt/build.zig.zon#L1-L65)、[Windows libc forwarding](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/crates/ghostty-vt/build.rs#L82-L94)。

`build.zig.zon` 带外部 URL 与内容 hash，包括 Ghostty dependency mirror、Codeberg translate-c 和本地 pkg。lazy dependency 不能全部算作本次编译的闭包，也不能假设 vendor 目录可完全离线构建。正式交付需记录实际解析/下载包和许可证，保存可复建缓存或可审核源包；Cargo.lock 无法锁住 Zig 的外部供应链。

本地 Ghostty patch 共六项，见[索引](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/libghostty-vt.patches.md#L1-L274)：

- `0002`：modifyOtherKeys mode 标量查询；输入协议有用。
- `0004`：Wuffs Windows translate-c 传递 libc 与 hosted allocator 修复；不能简单删掉，关 SIMD 的 Linux hosted 路径也可能依赖。
- `0005`：有界跨换行链接词选择；最小执行 API 不必暴露此产品能力，保留整绑定时仍有 ABI 引用。
- `0006`：保留光标行的清屏；需确认新绑定是否调用。
- `0007`：实验 PNG/不可变图像源持有；不是执行调度必要功能，却与现有 wrapper 的 native source、png forwarding 相连。删除它不是删 UI 文件即可，须同步删除 Rust/C ABI 使用并验证内存生命周期。
- `0008`：页面替换/分裂时保留 cursor hyperlink，修复可由终端输出触发的崩溃；即使不提供链接 UI，也应保留正确性修复。

先保留整个固定 Ghostty vendor 树用于可复现原型，再由链接结果、依赖图及专门测试决定源码裁剪。不能把 `-Demit-lib-vt` 解释为只携带几百行 parser。

## Windows 执行与恢复需要单独面对的成本

Windows 包的 ConPTY 来源固定为 Microsoft `Microsoft.Windows.Console.ConPTY` `1.24.260710001`，release `v1.24.11911.0`，nupkg SHA-256 `9382ad7becb7e4d84e300578d8e4f4df28f43d979d9055d978c42913c47e0e9d`。清单同时锁定 conpty.dll、x64 与 ARM64 OpenConsole、两个许可证/NOTICE 文件；它们不包含在 Linux ELF 体积中。[包清单](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/packaging/windows/conpty.json#L1-L48)、[受控加载实现](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/portable-pty/src/win/psuedocon.rs#L93-L279)。

特别区分：`StatusCommandGuard` 的 JobObject `KILL_ON_JOB_CLOSE` 用于状态脚本，创建时 suspended、加入 Job 后 resume，并非 pane PTY 的通用树监督器。pane 退出另走后代进程快照与信号流程。不能复制这段 guard 后就声称所有 ConPTY 宿主树及逃逸进程的恢复/关闭等价。[status guard](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/platform/windows.rs#L1294-L1430)、[pane shutdown](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/pane.rs#L1760-L1809)。

Herdr `HandoffRuntimeState` 与存活 master FD 的结构是 `cfg(unix)`；其注释也明确客户端请求、wait、订阅与消息不会随进程热移交，需重连/重试。Windows 不具备这个 FD 接管路径。`persist/restore.rs` 又依赖 workspace、tab、layout、agent resume plan 与多个 App runtime；若要求与完整 Herdr 等价的恢复，闭包会显著增大。[handoff state](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/handoff_runtime.rs#L1-L48)、[restore entry points](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/persist/restore.rs#L66-L120)、[workspace restoration](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/src/persist/restore.rs#L491-L587)。

原型合理的恢复下限：服务仍存活时客户端重连读回同一实例；服务代次变化时旧句柄明确拒绝，失去所有权的实例标记 lost/unknown；不自动重新执行已可能执行的输入。完整 crash recovery/热移交作为独立规格和异常实验，不能以“保存了 argv”替代。

## 可移除范围与真实耦合

| 范围 | 判断 | 具体成本 |
|---|---|---|
| TUI/workbench、复杂布局 | 不引入 App/PaneRuntime 时可排除 | `src/pane/terminal.rs` 同时有 Ghostty adapter 与 ratatui RenderState、选择/滚动/图像。最小层应直接调用 ghostty-vt，不能把整文件当纯 parser。 |
| remote/SSH/cloud/client codecs | 新的小本地协议可排除 | 不必复制 private TUI socket 与兼容 codecs；必须另保留本地权限、身份、请求上限与超时语义。 |
| 插件、通知、onboarding、update | 不引入 App/server 全闭包时可排除 | 通知是 platform/windows 子模块，完整 platform.rs 并非干净的系统工具库，直接复制会带大量 shell/clipboard/WMI/UI 功能。提取所需函数或实现窄平台层。 |
| 自动 agent detection/session discovery | 交给 WFC 宿主适配，原型可排除 | 不将 Herdr detector 当成内核调度职责；终端文本快照仍必须正确。 |
| 全量持久化/恢复 | 不能直接低成本摘出 | 现有恢复与 workspace/layout/agent/plugin 状态耦合。先定义较小恢复合同，超出时明确成本，而不是删掉正确性后冒称等价。 |
| 图像、选择和压缩 | 可后续探索，不先承诺剔除 | wrapper/Ghostty ABI、额外 patch 相互引用，需单独构建/行为验证；终端拒绝/限制图像资源也要明确合同。 |

这些结论由上述 imports/restore/patch 文件作出，是工程判断，尚未做逐符号最小链接闭包证明。

## 可复核源码规模

本地对固定 checkout 递归统计物理行（`bytes.splitlines()`）和文件字节；不计 `.git`、构建缓存、registry Cargo/Zig 外部下载。数值包含空白/注释，不是业务逻辑行数。

| 路径/范围 | 文件数 | 字节 | 物理行 |
|---|---:|---:|---:|
| `src/**/*.rs`，含内嵌及独立测试 | 402 | 10,465,617 | 291,081 |
| `crates/ghostty-vt/**/*.rs`，含 bindings 和测试 | 9 | 707,801 | 12,331 |
| `vendor/portable-pty/**/*.rs`，含 examples 与测试 | 12 | 104,620 | 3,232 |
| `vendor/libghostty-vt/**`，所有文件（含 Zig、C/header、pkg、测试、文档/许可） | 1,339 | 19,610,076 | 507,117 |

所以“Herdr 主 src 约 29 万行”和“Linux 二进制约 29 MiB”均不是最小内核规模。Ghostty Rust 绑定也不是 12k 全部生产代码：各文件首次 `#[cfg(test)]` 前的粗略合计 7,919 行、其后 4,412 行；该启发式不用于精确产品/测试拆分。最终保留源码、构建工具链、产物体积与完整 Herdr 对照须以隔离原型报告为准，Windows 产物与附加 ConPTY 包需另计。

## 许可证与来源保留清单

以下是技术分发来源清单，非法律意见；正式打包仍需核对实际构建闭包的许可文件。

| 来源 | 必须跟随记录/分发保留的依据 |
|---|---|
| Herdr Rust 代码/绑定/build.rs | [根 Apache-2.0 LICENSE](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/LICENSE#L1-L201)、crate manifest、源文件版权/署名、固定 commit、复制文件清单与本地修改记录。不要把 fork 的代码全部换成 WFC 作者署名。 |
| Ghostty | [MIT LICENSE，Mitchell Hashimoto 与 Ghostty contributors](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/libghostty-vt/LICENSE#L1-L21)、vendor provenance、六项 patch 索引/原文；vendor 中第三方 pkg 及实际下载依赖的各自许可证另行收集，不能只用顶层 MIT 概括全部依赖。 |
| portable-pty | [MIT LICENSE，Wez Furlong](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/vendor/portable-pty/LICENSE.md#L1-L21)、0.9.0 官方源包哈希、四项补丁来源、Cargo manifests。 |
| Microsoft ConPTY | [LICENSE](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/packaging/windows/licenses/Microsoft.Windows.Console.ConPTY-LICENSE.txt)、[NOTICE](https://github.com/herdrdev/herdr/blob/ee5bfb5041bb3e4a49d591c72308aae3c81ecdde/packaging/windows/licenses/Microsoft.Windows.Console.ConPTY-NOTICE.md)、nupkg 与每个分发文件哈希、package metadata；沿用上游 `THIRD-PARTY-NOTICES` 目标路径。 |
| Rust/Zig transitive deps | 保存最小原型 Cargo.lock、Zig manifests、下载来源/内容 hash 与实际许可证；不能拿完整 Herdr 主程序的依赖清单冒充裁剪后的闭包。SDK/CRT 与编译工具链分开记录，避免当作源码 MIT 的一部分。 |

建议正式接管前的决策票据：最小协议/身份与未知结果；终端行为/资源限额；各平台进程树监督与中断；服务重启/重连与恢复边界；可复建源包/补丁维护与许可证集合；Windows 原生运行验收。当前不移除 tmux、不迁移现有会话。若这些成本高于固定版本完整 Herdr 随包分发，应采用后者，并继续把宿主路径和登录作为独立配置问题。
