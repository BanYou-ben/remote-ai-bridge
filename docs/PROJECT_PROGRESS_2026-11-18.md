# Remote AI Bridge 项目进展与验证报告

日期：2026-11-18。适用范围：`feature/v0.5-ai-diagnostics` 的当前开发工作区，不代表已发布版本。

## 项目能力

Remote AI Bridge 在 Windows 上管理 SSH 反向隧道，使远端 Linux 通过本地代理访问外部服务。项目包含配置管理、Host Key 验证、进程身份校验、重连监督、分层诊断，以及 Vue / FastAPI 管理界面。

当前开发分支加入 LangChain AI 辅助诊断：FastAPI 使用 `ChatDeepSeek` 和 `create_agent`，通过受控 Registry 访问 18 个只读诊断工具。工具调用受 Profile Scope 约束；模型观察经过字段筛选，最终结构化诊断需要引用真实收集的 Evidence 并通过领域校验。原有 Doctor 可独立使用。

Web 页面采用深色控制台样式，包含连接配置、运行状态、设置向导、Doctor 和 AI 诊断区域。针对跨进程监督状态的歧义，页面区分进程存活、身份待核实与无运行记录；进程存活本身不证明端到端网络健康。

## 验证记录

- 用户提供的真实 DeepSeek 隔离探针结果：新版 LangChain 流程完成，`get_runtime_status`、`run_doctor` 成功，结构化诊断通过 Evidence 校验。工具结果与 Evidence 是模拟数据，并非真实 Linux 检查。
- RAB 反向隧道：在用户授权的现有 SSH 测试目标上建立 17890 反向转发；Doctor 报告本地代理、SSH 进程、远端监听与 Endpoint 网络检查通过。用户确认远程 Codex 恢复通信。HTTP 401 是无凭据探测到达服务端的网络证据，并非 API 认证成功的证明。
- 开发期间最新一次完整自动化回归：Python 正式测试 1003 项通过；前端 97 项通过且构建成功；额外 Spike / Validation 测试 34 项通过。各项数量分开统计。

## 尚未验证与限制

- LangChain AI 诊断使用真实 Linux 工具并经 Web 完成端到端诊断：未真实验证。隔离探针通过不等于所有模型行为或真实服务器诊断通过。
- 模型对敏感内容的自动筛选不保证识别所有秘密；用户需要明确同意数据外发，且不应在诊断问题中输入凭据。DeepSeek 调用可能产生费用。
- FastAPI 仅供本机回环使用，不应直接暴露到局域网或互联网。
- 监督进程是进程内的：由其他 RAB 前台进程维持的隧道可能不受网页后端进程监督。页面中的进程存活不等于完整网络健康，需结合 Doctor 判断。
- 旧运行记录在进程身份不匹配时必须保留并安全处理；不能根据端口号终止未知进程。

## 复现与材料

项目依赖与启动步骤见 [中文版 README](../README_zh-CN.md)，架构和面试说明见 [项目交付说明](PROJECT_HANDOFF_ZH.md)，AI 验收步骤及安全边界见 [LangChain 验收报告](LANGCHAIN_ACCEPTANCE_REPORT.md)。
