# DeepSeek Web 诊断集成报告

## 功能

- Dashboard 增加 AI 诊断面板：选择 Profile、输入问题和一次性 DeepSeek API Key，勾选同意后运行。完成后展示工具执行记录、诊断类别、摘要和 Evidence 状态。工具记录在请求结束后展示，不是实时流式进度。
- FastAPI 新增本地 `POST /ai/diagnose/{name}`，要求明确 consent，并校验 loopback Host 和 Origin；同时只允许一个 AI 诊断运行。服务同步执行于 FastAPI 的线程池，不会自动启动、停止或断开 Tunnel。
- 复用既有 AgentRunner、只读 ToolRegistry、DeepSeek Responses Adapter 和 Evidence grounding；模型看到的 Profile 名使用固定别名，返回的工具调用必须通过原有 Profile Scope 检查。
- 模型只看到 14 个连接、网络和系统类只读工具；不暴露配置摘要和 application target 工具。工具 observations 只发送固定 Evidence ID、状态和安全的少量数值指标，原始 detail、用户名、主机地址、PID、进程名与服务名不作为工具数据发送。
- AI 失败时页面提示使用既有 Doctor。现有 CLI、Runtime 和 Doctor 未被替换。

## 数据和安全限制

- 用户输入的自然语言问题经过现有 redaction、已知 API Key、URL、IPv4、部分路径及当前 Profile 名处理后发送给 DeepSeek；这些规则**不能保证识别所有个人信息、主机名、凭据或隐私**。用户须在输入框中自行避免填写敏感数据，并明确勾选数据外发及费用提示。
- API Key 通过本机页面密码框输入，经 loopback HTTP 发往本地 API；不会写入配置或日志，前端提交时清空输入框。Python/浏览器内存、系统或浏览器扩展不能保证安全擦除；请只在可信本机使用。
- 本地 API 没有完整用户认证。Origin/Host 检查不是对恶意本地程序或浏览器扩展的防护；不得将服务暴露到局域网或互联网。
- 模型的诊断是建议而非操作；固定只读工具仍可能执行实际 SSH、TCP 和 GET 请求，需用户授权目标与 endpoint。最终结果文本也需审阅，不能视作绝对无敏感信息。
- `ok` 表示工具处理完成，不等同于 Evidence PASS。当前状态和指标仅覆盖预设白名单，application target 诊断暂未接入 AI 页面。
- 同步请求超时或浏览器关闭不一定停止后端已有任务；服务器内的单运行门闩防止本进程并发重复诊断，但不跨多个进程。使用默认单 worker。

## 离线验证

- 正式 pytest：991 passed；validation 测试：26 passed。
- 前端 Vitest：93 passed；Vite production build：PASS。
- 测试覆盖模型输入字段最小化、Profile 别名与越权、禁止未授权工具、同意与来源检查、单运行门闩、超时降级、Evidence grounding、密钥输出保护、前端确认与去重；另通过真实 OpenAI SDK + 本地 MockTransport 覆盖 Web API 到多轮诊断的离线链路。
- 用户此前报告真实 DeepSeek + 内存模拟工具隔离测试成功；本次 Web 集成、真实 Profile、Linux SSH/Doctor 及完整端到端未真实验证。未发起付费 API、未连接真实服务器。

## 用户授权后的验证方法

启动本地后端（项目根目录）：

```powershell
D:\anaconda\envs\BY\python.exe -m app.cli serve
```

另开终端启动前端：

```powershell
cd web
npm run dev
```

在本机打开 Vite 提示的 `127.0.0.1` 地址。用户自行确认被测试的 Profile、对应 Linux 账号、所有相关 GET endpoint 和数据发送范围均获授权；选择 Profile，输入不含敏感信息的问题及 Key，阅读提示并勾选同意后启动。不要把 Key 发给助手。每次最多 6 次模型决定，SDK 自动重试为 0；工具执行可能产生额外 SSH/TCP/HTTP 流量，具体费用和次数以实际情况为准。

若仅希望验证页面或后端接口，应使用 fake model 与 mock 数据；本报告不把该离线结果视为真实服务验收。
