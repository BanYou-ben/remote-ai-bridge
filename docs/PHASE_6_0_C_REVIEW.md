# Phase 6.0-C：DeepSeek 验证入口确认与结果展示

## 改动

- 入口在读取密钥、创建服务及发送模型请求前，展示模型名称、可能产生费用、问题/工具观察/Evidence 将发送至 DeepSeek；只有明确输入 `YES` 才继续。拒绝、空输入和 EOF 均取消。
- 结果展示执行状态、步数、错误码、已调用工具名称和成功状态；有最终诊断时展示类别、引用的 Evidence ID 和摘要。工具名称只显示当前注册的名称或 `[unknown]`，不展示工具参数、原始 observations、工具输出和异常详情。
- 摘要在截断前经过现有 redaction，并移除本次 API key、常见密钥格式、URL、部分绝对路径及 IPv4；再过滤不可打印字符并限制长度为 500 字符。类别与 Evidence ID 也经同一处理并限制长度，Evidence ID 数量上限为 32。脱敏属于尽力防护，不能保证识别所有未知密钥或敏感信息，终端输出也不应视为适合无条件公开。
- 保留现有 AgentRunner、ToolRegistry、18 个诊断工具和 DeepSeek Responses Adapter；不改变 SSH、Runtime 或 Doctor。

## 离线验证

- Fake Responses 多步调用测试检查工具状态、最终诊断类别、Evidence ID 和摘要。
- 参数化测试检查未确认不会创建服务或发起模型请求；EOF 同样取消。
- 测试摘要中的本次 key、密码、URL、路径和 IP 被遮盖，并检查摘要长度、工具原文不被展示。
- 相关测试：87 passed；完整正式 pytest：953 passed。validation 测试位于 `tools/validation`，需单独指定运行。

## 限制

- 没有调用真实付费 API，尚未验证真实 DeepSeek 响应、strict schema 兼容性或诊断质量。
- 用户问题、工具观察和 Evidence 会发送给 DeepSeek；输出侧脱敏不改变发送到模型的内容。执行前应由用户确认数据范围。
- DeepSeek 文档指出 `parallel_tool_calls` 和 `max_tool_calls` 会被忽略；现有 Adapter 对多个 function call 采用本地 fail-closed 拒绝。
