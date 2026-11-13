# Router research validation · 2026-11-13

身份：研究与文档留存验证，不是 router 功能实现或外部集成验收。

## 实际执行

- 七个公开仓库 shallow clone 并固定 HEAD：Orca、Agent Autoloop、LobeHub、Herdr、Octopus、Paseo、Codeg。
- 阅读各自核心消息/执行/恢复/平台代码和许可证；对照本仓 router、打包与测试。固定对象、相关文件 SHA-256 由[来源清单](research-source-pins-20261113.json)记录。
- 下载并只读 Pachat 官方公开客户端与安装协议，记录内容摘要；未找到可核实服务端仓库，客户端再分发许可未确认。
- 阅读 Tern.build 官方页面和 CLI 文档；其官方声明闭源。直接下载 installer/docs 的尝试返回 403，没有把可读网页当核心源码。
- 用户确认产品定位、上层工厂关系、Windows 期望及资料入库；定位记入[决策日志](decision-log.md)，实现顺序仍为建议。

## 未执行

没有安装或运行外部产品、Agent、listener 或 daemon；没有配对账号、发信、注册后台服务、修改宿主权限或重启现有工作。未运行上游测试、未进行 Windows 真机或 WSL 演练、未确认上游最新 CI 全部通过。

没有修改 router.py、wfc.py、build.py、测试或工作流；不把“Windows 分支/CI 定义存在”写成 wfc 已支持原生 Windows。不把历史 probes 的结果扩大到本轮固定的新源码。

## 本轮文档核对

以下核对围绕新研究与 README/决策日志补充，不复写历史探针或改变产品行为：

- 主控实际核对 11 份本轮文件中的 89 条相对链接、83 条固定源码链接（含目标对象与行号），0 错误；来源清单的七仓 61 项文件 SHA-256 全部与固定 Git 对象一致。
- 新文件逐项空白检查和 tracked diff --check 通过；新文档公开内容模式检查 0 命中。独立审阅又核对七仓 HEAD、61 项哈希、相对链接、平台/来源身份与核心边界，未发现阻塞问题；指出的验证记录占位已填实。
- 单元测试/打包：文档变更不重跑；现有 CI 仍只覆盖其原配置。
- 本地留存：独立分支 `docs/router-foundation-20261113`，审阅完成后保存本地文档提交；用该分支的 git log 核对提交身份。没有推送、PR、合并或 Release。

## 接手入口

从[文档索引](README.md)与[Router foundation](router-foundation.md)开始。源码 clone 和聊天记录可供进一步追溯，但继续工作所需的产品定位、版本、证据链接、待补顺序与验证边界已保存在本仓。
