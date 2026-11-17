# Phase 6.0-B：DeepSeek Agent 验证入口

## 范围和状态

新增 `tools/validation/agent_model_validation.py`，复用现有 `create_services()`、ToolRegistry、`OpenAICompatibleResponsesAgentModel` 和 AgentRunner。未修改 SSH、Runtime 或 Doctor，未新增诊断工具或依赖。未调用真实 DeepSeek API；真实兼容性和端到端效果尚未验证。

入口默认为 `deepseek-flash` 和固定 `https://api.deepseek.com`，支持 `--model`、`--profile`、`--question` 和 `--max-steps`（1–12，默认 6）。API Key 从 `DEEPSEEK_API_KEY` 或本机 `getpass` 读取，不通过命令行参数传递、不写文件或日志。入口仅打印运行状态、步数、错误码及是否存在最终诊断，不输出模型或工具原文。Python 字符串及环境变量不保证在内存中被安全擦除。

将来经用户确认后可在项目根目录运行：

```powershell
python -m tools.validation.agent_model_validation --profile <profile-name> --question "检查当前连接状态" --max-steps 4
```

此命令会访问真实 API、可能产生费用，并将问题、工具观察与证据发送给 DeepSeek；仅在确认数据范围与费用后使用。本阶段并未执行该命令。环境变量由用户在本地安全管理，也可不设置变量，启动后使用终端隐藏输入。不要把密钥放进 `--question`。

## 官方文档兼容性检查

依据 [DeepSeek Responses API 指南](https://api-docs.deepseek.com/guides/responses_api/) 和 [Responses API 参考](https://api-docs.deepseek.com/api/create-response/)：

- `deepseek-flash`、指定 base URL、function tools 和 `tool_choice="required"` 在文档中列为支持。
- `parallel_tool_calls` 被忽略，并行调用始终启用；`max_tool_calls` 也被忽略。当前 Adapter 在任何工具执行前要求响应中恰好一个 function call，多个调用返回 `AGENT_MODEL_INVALID_RESPONSE`，不执行其中任何一个。可能因此无法完成某些真实请求，属于已知可用性风险。
- `store` 在文档中列为不支持，响应固定为 false；服务为无状态。当前 Adapter 每轮将观察和证据作为 JSON input 重新发送，并不依赖服务端会话。
- Responses 文档未明确确认工具 `strict` 字段及当前完整 JSON Schema 的支持。[Tool Calls 指南](https://api-docs.deepseek.com/guides/tool_calls/) 对 Chat Completions 格式的 strict beta 有单独说明，不能据此认定 Responses 的 strict 行为。尤其空 evidence enum、`pattern`、`uniqueItems` 和数组约束尚需实测；本地参数校验与 Evidence grounding 仍然有效。

## 测试

- Fake Responses 多步测试：调用 `run_doctor`，下一轮收到观察和 Evidence，再调用 `submit_diagnosis` 并完成 grounding。
- 多个 function call、超时、未知异常、密钥输出保护、禁止密钥 argv、步骤范围和 getpass 失败路径均有针对性测试。
- 定向测试：82 passed；完整正式 pytest：953 passed。新增 validation 测试位于 `tools/validation`，需单独指定运行，不属于 `pyproject.toml` 默认 `tests/` 收集范围。

## 已知限制

- 真实 DeepSeek Responses、strict schema、模型是否能遵循当前每轮 JSON observation 回传约定，以及真实费用、时延和输出质量均未验证。
- 工具观察和 Evidence 被发送给供应商；现有脱敏不能证明识别所有敏感信息。入口不会打印完整诊断文本，因而还不能独立提供完整的人工诊断质量审阅界面。
- 多调用拒绝是 fail-closed，不会自动选择其中一个；模型连续返回多个调用时本次验证会失败。
