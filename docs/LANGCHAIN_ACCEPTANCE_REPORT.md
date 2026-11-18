# LangChain 迁移验收报告

## 结论

离线验收通过。用户报告新版 LangChain 链路使用隔离模拟数据调用真实 DeepSeek API 成功；真实 Linux 服务器及浏览器端到端验收仍**未真实验证**。真实服务器操作需要用户另外明确授权。

用户提供的隔离探针结果：`status=completed`、`error_code=null`、工具依次为 `get_runtime_status` 和 `run_doctor` 且均成功、`grounded_diagnosis=true`。这是用户运行探针后提供的结果，工具数据和 Evidence 均为模拟数据，并非真实服务器验证。

FastAPI `/ai/diagnose/{name}` 的生产工厂创建 `ChatDeepSeek`，由 `run_web_diagnostics` 进入 LangChain `create_agent`。18 个现有只读工具经 `LangChainToolBridge` 调用 `ToolRegistry`；Profile Scope、目标别名和结果最小化在执行层实施。最终结构化结果须通过 Evidence ID 和 category grounding。Doctor 不依赖模型，可以单独使用。

旧的 `tools.validation.deepseek_isolated_probe` 使用旧 Responses 适配器，不能证明当前 LangChain Chat Completions 链路有效。本次新增 `tools.validation.langchain_deepseek_probe`，使用生产模型工厂、生产 LangChain 编排和两个固定模拟工具，不访问 SSH 或真实 Profile。

## 本次离线结果

- Python 正式回归：1003 passed（tool_choice 兼容性修复后）。
- 独立 Spike / Validation（含新增探针测试）：34 passed。
- 定向 LangChain、Web API、探针测试：20 passed。
- 前端：93 passed；生产构建通过。
- 新增探针取消时不读取 Key、不构造模型；固定模拟数据测试不输出 Key。
- 实际安装的 LangChain 图、ChatDeepSeek HTTP mock、多轮工具、Profile Scope、并行工具拒绝、超时、结构化输出与虚构 Evidence 拒绝已有离线测试；这不是在线 API 测试。

## 真实验收准备（须先授权）

隔离探针：在项目目录执行：

```powershell
.\.venv\Scripts\python.exe -m tools.validation.langchain_deepseek_probe
```

它显示费用和数据外发提示；只有输入大写 `YES` 后才会通过本地 `getpass` 请求 Key。隔离探针最多四次模型请求，每次最多 1024 输出 token，30 秒超时，SDK 重试为零；图的递归限制可能更早停止。仅固定问题、模拟工具结果与模拟 Evidence 发往 DeepSeek。成功需检查 completed、工具执行结果和 grounded_diagnosis，失败不等于 Key 一定错误。不要把 Key 发到聊天或命令行参数。

隔离探针通过并取得独立的 Linux 测试授权后，使用已有测试 Profile。在项目目录分别启动本地后端与前端：

```powershell
.\.venv\Scripts\python.exe -m app.cli serve
```

```powershell
cd web
npm run dev
```

打开 `http://127.0.0.1:5173`，选择测试 Profile，核对连接状态和 Doctor，再在 AI 故障诊断中填写不含秘密的问题，在页面内输入 Key 并勾选数据外发及费用同意。确认工具记录、Evidence、结论和错误状态；单次 AI 诊断同样最多六次模型请求。传统 Doctor 可以单独使用。SSH 认证和已有诊断命令只使用当前用户已有权限，不需要 sudo；Agent 仅能调用已注册的受控工具。测试不会主动断开、重启或修改远程服务。

可能发送问题、工具名称、匿名 Profile / 应用目标标识、有限运行状态、数值指标与 Evidence ID / 状态。问题中的任意敏感信息无法保证被完全自动识别，使用者应自行避免输入。不要将页面或 API 服务暴露到局域网。

## 待解决与边界

当前 ChatDeepSeek 使用 Chat Completions。用户报告的本次隔离探针验证了该次请求中模型调用、工具执行和 Evidence 校验；不能推断真实服务器、全部模型行为或浏览器完整流程均已验证。真实请求次数和费用取决于模型选择和账户定价。真实服务器测试可能执行现有只读检查及网络探测；需用户授权范围覆盖这些访问。Git 提交与推送状态以当前分支为准。
