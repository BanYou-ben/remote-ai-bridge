# DeepSeek Agent 集成验证报告

> 本报告为真实隔离测试成功前的离线审查记录。后续结果见 [Phase 6.0-D 报告](PHASE_6_0_D_REVIEW.md)：用户已报告模拟工具和真实 DeepSeek API 的三轮验证成功，真实 Linux 服务器仍未验证。

## 结论

已完成不连接外部服务的集成检查和回归。真实 DeepSeek Function Calling、多轮诊断、真实 Linux 服务器访问均**未真实验证**；目前不能声称真实端到端完成。

## 已实现和本次修改

- 复用现有 AgentRunner、ToolRegistry、Responses Adapter 与 validation 入口。Agent 每轮使用工具观察和 Evidence 再次请求模型；最终诊断由本地验证 Evidence ID 和 category。
- Adapter 对服务端 `failed` 状态返回安全的不可用错误；响应包含非预期 custom、web search 或 tool search 调用时整次拒绝，不执行其中工具。既有的多个 function call fail-closed 保护保持不变。
- validation 入口在 Agent 失败时提示可手动运行现有 `rab doctor <profile>`；不会自动执行 Doctor 或连接服务器。
- 新增本地 HTTP 合约测试，通过 OpenAI SDK + `httpx.MockTransport` 验证请求序列化、两轮 function calling、工具观察回传、Evidence grounding、多调用拒绝、越权、401/429/500 和非预期工具类型。MockTransport 不发网络请求。

## 文档核查和兼容风险

DeepSeek [Responses 指南](https://api-docs.deepseek.com/guides/responses_api/) 列出 `deepseek-flash`、function tools 和 `tool_choice=required` 支持，并说明 `parallel_tool_calls` 和 `max_tool_calls` 被忽略，`previous_response_id` 不支持。当前本地 Adapter 会拒绝多工具调用，不会自动执行额外工具，但这可能使某次真实请求失败。`strict` 与本项目完整 schema（尤其空 Evidence enum 和数组约束）在 Responses 上的兼容性尚未实测；[Tool Calls 指南](https://api-docs.deepseek.com/guides/tool_calls/) 中 strict beta 的 Chat Completions 说明不能直接证明 Responses 支持。

当前每轮发送完整 JSON 状态与 observations，而非标准 `function_call_output` 消息；模型能否可靠利用该格式必须真实测试。现有代码在用户同意后会向 DeepSeek 发送问题、工具 observations 和 Evidence；脱敏不能保证识别所有敏感信息。

## 离线结果

- 相关定向测试：95 passed（包含 validation 测试）。
- 完整正式 pytest：961 passed。默认 pytest 收集 `tests/`，不包括 `tools/validation/`。
- 测试未读取真实 API Key、未调用付费 API、未访问真实服务器。

## 后续用户授权后的最小真实验证

1. 用户自行在 Windows 上准备已授权且已验证的 Profile、可用代理、DeepSeek 账户及测试预算。不要把 API Key 发给助手或写入命令行；由用户在入口提示时本地隐藏输入，或自行通过环境变量提供。
2. 在项目目录运行以下命令，替换 Profile 名称：

   ```powershell
   D:\anaconda\envs\BY\python.exe -m tools.validation.agent_model_validation --profile <profile-name> --question "检查当前连接状态" --max-steps 4
   ```

3. 阅读入口的费用和数据外发提示，用户仅在同意后输入 `YES`。确认至少一次真实只读工具调用、下一轮基于观察继续、最终 `completed` 且 Evidence ID 与诊断领域匹配。不要分享密钥或未脱敏网络信息。
4. 失败时记录安全错误码；需要传统诊断时用户可执行 `rab doctor <profile-name>`。该命令会访问 Profile 所指向的实际环境，应取得相应授权。

本阶段没有自动 commit 或 push。真实服务兼容性、真实模型质量和真实服务器诊断均仍待明确授权与实际验证。
