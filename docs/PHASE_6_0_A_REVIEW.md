# Phase 6.0-A：真实模型接入审查与规划

审查基线：`feature/v0.5-ai-diagnostics`，提交 `3219654aae0beab199982e31fdfe053dc11b47de`。本报告依据该版本实际代码；本阶段未调用真实模型或连接 Linux 主机。

## 当前代码能力

- `AgentModel` 定义 `decide(request, state, tools)`，返回工具调用或最终诊断决定。`AgentRunner` 默认最多执行六轮决定，每轮只向模型暴露只读工具；工具执行前再次校验风险等级和 Profile scope。
- `ToolRegistry` 对工具参数进行严格验证，检查 ToolResult 类型和工具名，对未知工具、无效参数及执行异常返回结构化结果。
- `create_services()` 创建并暴露共享的 `agent_tool_registry`。当前包含 18 个只读工具：connection 8、network 1、system 6、application 3。Application 工具还检查 Target 与 Profile 的绑定。
- 工具 Evidence 汇集到统一 catalog；最终 `StructuredDiagnosis` 必须引用存在的 Evidence，除 `unknown` 外，诊断一级领域必须与至少一个被引用 Evidence 的 category 一致。
- `pyproject.toml` 已声明 `openai==3.19.2`。现有测试使用 fake Responses 客户端与 scripted AgentModel，覆盖协议解析、错误处理、工具和多步诊断，但不能证明真实服务兼容或真实主机效果。

## Adapter 实际协议

`OpenAICompatibleResponsesAgentModel` 使用 OpenAI SDK `client.responses.create()`，不是 Chat Completions。每次 `decide()` 发送模型名、instructions、JSON 字符串 input、function tools，以及 `tool_choice="required"`、`parallel_tool_calls=False`、`max_tool_calls=1`、`store=False`、`max_output_tokens=1200`。工具 schema 带 `strict=True`。

Adapter 要求响应恰有一个 `function_call`，其 `name` 属于当前允许工具或 `submit_diagnosis`，`arguments` 为可解析为 JSON object 的字符串。`submit_diagnosis` 是应用自定义 function：它的 Evidence ID 枚举由当前 Evidence 生成，最终仍经过本地结构和 grounding 验证。

多轮由本地 Runner 驱动：每轮执行至多一个工具，再把历次 `TOOL_OBSERVATIONS`、`COLLECTED_EVIDENCE`、请求与 step count 序列化到下一次请求的普通 input 中。当前不使用 `previous_response_id`，也不以标准 `function_call_output` 消息关联服务器端会话。服务端不必保存会话，但模型必须能依据这种输入继续调用工具。

超时、连接错误、其他 API 错误、拒绝、incomplete 和无效响应会转换为 `AgentModelError` 的安全错误码；Runner 保留该码。默认单次请求超时 60 秒、SDK 重试 1 次；`AgentRunResult` 当前不包含 retryable。默认六轮中最终提交也消耗一轮，最多可先执行五轮工具后再提交。模型可能提前提交、反复调用或达到上限。

## 缺失功能与最小方案

当前 CLI、API 和 validation tools 中没有连接真实 Agent 的用户入口。真实模型名、base URL、凭据输入与运行结果展示尚无统一的验证流程；真实 Responses 服务兼容性也未验证。

建议下一阶段新增隔离的 `tools/validation/agent_model_validation.py` 和相应测试，例如 `tools/validation/test_agent_model_validation.py`：

1. 接收 Profile 名称、诊断问题、模型名、base URL 和受限步数；在用户主动执行时通过本地终端临时读取凭据，不写入 argv、日志、Profile、Runtime 或仓库文件。
2. 复用 `create_services()` 的 registry，构造现有 Adapter、AgentRunner 与 AgentRequest，展示安全处理后的观察、Evidence、诊断和结构化错误。
3. 明示真实请求可能产生费用，并在入口执行前确认将发送的诊断数据范围。不要默认打印原始请求、工具输出或 SDK 异常。
4. 先使用注入的 fake Responses 客户端进行离线测试；只有确认供应商和用户主动执行后才进行真实模型验证。

预计新增上述两个验证文件即可完成第一版入口；是否修改 Adapter 应以具体服务的兼容性验证为依据。本阶段不提议重写 Runner 或修改 SSH、Runtime、Doctor。无需引入新框架或新诊断工具。

## API 兼容性要求

所选供应商与具体模型必须支持上述 Responses 请求字段、function calling、严格工具 schema、单次调用限制，以及包含 JSON object 的 function arguments。仅标称“OpenAI-compatible”不足以证明兼容：只支持 Chat Completions 的服务不能直接使用当前 Adapter。

尤其需核验 provider 对 `tool_choice="required"`、`max_tool_calls`、`store`、`parallel_tool_calls`、schema 的 `pattern`、`uniqueItems`、数组数量限制以及空 Evidence 时空 enum 的处理。当前还未选择或验证具体供应商；不得预设上述字段均受支持。工具观察通过普通 JSON input 传回，需验证模型能正确理解该多轮约定。

## 最小端到端执行流程

1. 离线检查入口的参数、凭据生命周期、错误脱敏、工具暴露、观察回传、最终 Evidence grounding 和最大轮数。
2. 准备用户授权的测试 Profile，确认其 SSH Host Key、网络与只读诊断前提；先完成下方 Phase 5.9 Linux 验证。
3. 核对所选供应商的具体协议、模型能力、数据处理政策、费用与限额；在安全环境中由用户主动启动验证。
4. 构造服务、Adapter 和 Runner；输入简单诊断问题；观察至少一次真实工具调用、下一轮观察回传与 `submit_diagnosis`。
5. 核查结果 `completed`、Evidence ID 和诊断领域匹配；记录模型名、调用轮数、结果与必要费用信息，但不记录凭据或敏感原始数据。若失败，区分协议、认证、网络、工具和 grounding 错误，不把 fake 测试作为实机通过。

## Phase 5.9 Linux 最小只读验证清单

使用明确属于测试 Profile 的持久化 trusted Target；先由用户确认目标服务和 loopback endpoint，测试不创建或修改服务。需验证：

1. 只读 `systemctl show`：已知 active 单元返回 PASS；已知不存在或 inactive 单元返回可解释结果，并记录实际 exit code 与 `LoadState`/`ActiveState`/`SubState` 的行为。
2. 只读 health GET：已知 2xx 返回 PASS；可控非 2xx、超时或不可达返回对应 FAIL；不保存响应正文。仅在用户确认测试 endpoint 的 GET 无副作用后执行。
3. 在可控 3xx endpoint 上确认只读取初始状态，未跟随 Location；验证远端设置代理变量时 loopback 请求仍绕过代理，URL glob 不展开。
4. 验证缺少 service/health 配置时为 SKIP；不同 Profile 的 Target 被拒绝且不执行 SSH/HTTP。以受控数据核查输出不包含凭据、命令、stderr 或响应正文。
5. 确认通过现有 SSH prefix 使用 BatchMode、严格 Host Key 校验与现有密钥；不修改 sshd 配置，不重启、停止或修改远端服务。

上述均为计划，**尚未真实验证**。已有 Phase 5.9 Review 记录指出：systemctl 非零退出目前可能把权限等其他原因归为服务管理器不可用；应结合真实系统结果评估，避免误称根因。

## 主要风险与测试方案

- **协议差异**：Responses 参数、strict schema 或空 enum 可能被拒绝。先做本地 contract 测试，再针对选定服务做一次受控兼容性验证；不得在未确认兼容时承诺适配。
- **数据外发**：用户请求和观察、Evidence 会发送给模型服务。检查脱敏不能视为绝对保证；正式运行前确认数据内容、服务条款与日志策略。
- **费用与时间**：每轮一次请求，SDK 重试和工具执行可增加成本与耗时；验证入口需提供有限步数、超时及明确的费用提示。
- **模型不稳定**：非法 JSON、多个调用、拒绝、伪造 Evidence、跨 Profile、提前提交或达到步数限制均应使用 fake provider 覆盖，并保持本地拒绝行为。
- **环境与网络**：SSH、systemd、curl、权限及 loopback 服务可能与 fake 测试不同；按上方清单单独验证，不将服务故障误判为模型故障。
