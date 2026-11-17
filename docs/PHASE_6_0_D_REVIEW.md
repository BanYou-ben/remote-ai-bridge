# Phase 6.0-D：DeepSeek API 隔离兼容性验证准备

## 当前结论

用户自行运行隔离验证，最新报告 `status=completed`、`error_code=null`、两个预期工具均成功、`grounded_diagnosis=true`、`expected_sequence=true`。因此，真实 DeepSeek API 配合内存模拟工具的三轮调用和最终 Evidence 校验已通过。真实 Linux 服务器及其诊断链路仍**未真实验证**。助手没有调用付费 API、访问真实服务器或读取仓库外凭据文件。

新增 `tools/validation/deepseek_isolated_probe.py`，复用现有 Responses Adapter 和 AgentRunner；它只注册现有工具名的内存测试替身 `get_runtime_status` 与 `run_doctor`，不增加生产 ToolRegistry 工具、不读取 Profile、不运行 SSH。固定 Evidence 为 `connection.ssh.config`，其数据完全为测试虚构数据。

预期三个连续决策：模拟 runtime 工具 → 模拟 doctor 工具 → `submit_diagnosis`。Runner 要求诊断引用实际收集的 Evidence 并通过领域校验；入口另外检查工具调用顺序、成功状态和 Evidence ID。模型提前结束、调用其他工具或返回多个 function call 都可能导致验证失败，不能将预期当作实际结果。

## 离线验证

- 定向测试：72 passed，包含假 Responses 客户端的三步流程、观察回传、未确认时不读取 key/创建模型、多工具调用拒绝、超时和 HTTP 错误分类。
- 完整正式 pytest：966 passed；`tools/validation` 测试需单独指定。
- 当前未提交的既有集成改动和报告仍保留。新增安全错误码区分连接、超时、401 认证、402 付款、403 权限、400/404/422 请求、429 限流和 5xx 服务错误；不打印原始响应或异常。分类只表示状态码，不能证明具体根因。没有因缺少真实 API 响应而假定或修改供应商参数兼容性。

## 未来受限真实验证

在用户明确授权付费请求后，由用户在本机项目根目录执行：

```powershell
D:\anaconda\envs\BY\python.exe -m tools.validation.deepseek_isolated_probe
```

入口先显示费用和数据说明；只有输入 `YES` 才继续，然后在本机通过 `getpass` 输入 DeepSeek API Key。不要在聊天、命令参数或日志中提供密钥。本机需具备 BY 环境及可连接 DeepSeek 的网络。

请求上限：**最多 3 次 Responses API 请求**，SDK `max_retries=0`；模型提前失败、拒绝、多调用或诊断无效时可能少于三次。用户的真实请求可能产生费用；本报告未取得或核算费用和 token 用量。

数据外发范围：固定英文测试问题、固定 Profile 名 `fixture-server`、两个模拟工具定义和返回的固定测试观察与 Evidence、诊断 schema。不会发送真实 Profile、真实服务器信息或 SSH 凭据。API Key 用于向 DeepSeek 认证，不作为问题或测试数据发送；不要把密钥发给助手。输出仅包含状态、错误码、受限工具名、成功状态与 grounding/顺序结果，不打印模型摘要、原始工具返回或密钥。

## 已知风险

DeepSeek [官方 Responses 指南](https://api-docs.deepseek.com/guides/responses_api/)说明 `parallel_tool_calls` 和 `max_tool_calls` 被忽略；本地 Adapter 将多个 function call 全部拒绝。strict schema 和空 Evidence enum 在真实 Responses 上是否受支持仍未验证；一次隔离成功不代表所有模型输出或主机环境均兼容。

## HTTP 400 后的离线修复

用户重试得到 `AGENT_MODEL_BAD_REQUEST`，但未获得服务器提供的具体参数原因。当前请求在尚无 Evidence 时不再暴露 `submit_diagnosis`，避免发送空 Evidence enum；收集到 Evidence 后才开放最终诊断。模型即使擅自请求提前提交也会被拒绝，本地 Evidence grounding 仍然保留。错误分类只在响应的 `param` 字段明确标识 `tools` 或 `model` 时提供固定分类，不输出服务端原文。此项修复是否解决真实 400 尚未验证。

修复后的离线定向测试：109 passed；完整正式 pytest：969 passed。

用户后续实测报告 `AGENT_MODEL_BAD_REQUEST_TOOL_CHOICE`。该类别基于服务端错误消息中的固定关键词，表明错误信息提及 `tool_choice`，但不证明具体服务器原因。DeepSeek 验证入口现使用 `tool_choice=auto`；Adapter 本地仍要求且只接受一个已允许的 function call，不执行多个调用或自由文本。其他 provider 默认仍为 `required`。该修改时相关离线测试 119 passed，完整 pytest 978 passed。

之后用户真实测试中，两次工具调用成功，但最终诊断返回不符合格式的 `diagnosis_stage`。适配器已向模型明确说明小写点分格式，保留 confidence 和 Evidence ID 枚举，且不放宽本地校验。相关离线测试 127 passed，完整 pytest 978 passed。用户随后自行运行真实隔离验证并报告完成：工具依次为 `get_runtime_status` 和 `run_doctor`，二者成功，最终诊断 Evidence 校验通过。该结果由用户终端提供；本助手没有取得供应商原始响应、费用或 token 用量。
