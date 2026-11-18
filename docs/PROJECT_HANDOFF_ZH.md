# Remote AI Bridge 项目交付与面试说明

## 项目定位

Remote AI Bridge 面向 Windows 本地代理与远程 Linux 应用的连接管理：通过 SSH 反向隧道连接两端，并提供配置、运行监督和分层诊断。当前开发分支新增 AI 辅助诊断。它不是 VPN，也不提供任意远程命令执行。

## 核心调用链

1. Vue 页面向本机 FastAPI 接口提交用户确认后的诊断请求。
2. 服务创建 ChatDeepSeek，由 LangChain `create_agent` 编排诊断；模型收到经过筛选的问题和工具观察。
3. 18 个只读工具经桥接层调用已有 ToolRegistry。桥接层检查允许的工具、Profile Scope，并将应用目标替换为会话内别名。
4. Doctor、Runtime、网络、系统及应用服务返回受控检查结果；发送前只保留允许的 Evidence 与部分状态或指标。
5. LangChain 返回 Pydantic 结构化结果；本地校验引用的 Evidence ID 存在，且诊断领域与对应 Evidence category 相符。
6. API 返回工具记录、证据和诊断摘要给 Vue。失败时可以独立使用传统 Doctor。

系统保留 SSH Host Key 确认、回环绑定和进程所有权校验。AI 工具不提供任意 Shell、远程修改或服务重启功能。具体限制及已知风险见 [验收报告](LANGCHAIN_ACCEPTANCE_REPORT.md)。

## 从源码启动

在 Windows PowerShell 项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m app.cli serve
```

另开终端：

```powershell
cd web
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。前端开发环境按锁定依赖要求使用 Node.js `^22.22.2 || ^24.15.0 || >=26.0.0`。FastAPI 仅绑定本地回环。首次连接和 SSH 配置参见 [中文版 README](../README_zh-CN.md)。

## 测试与真实验证边界

最近一次离线结果：Python 正式测试 1003 项通过，独立 Spike / Validation 34 项通过，前端 97 项通过且构建成功。定向测试实际运行 LangChain 图，并通过模拟 HTTP 传输调用 ChatDeepSeek 适配器；不会仅凭模拟测试证明真实服务兼容。

已发布 Phase 1/2/4 的历史真实环境记录与本次开发分支的验收分开看。用户报告新版 LangChain ChatDeepSeek 在隔离模拟数据上的真实 API 探针成功；真实 Linux 和浏览器完整流程尚未验证。后续只读 Linux 验收命令见 [验收报告](LANGCHAIN_ACCEPTANCE_REPORT.md)，需先取得用户授权；API Key 只在本机输入。

## 简历表述素材

以下是可核验的项目事实，请按本人实际参与内容改写；不要把他人或自动化工具完成的工作全部声称为个人独立完成。

- 测试开发：围绕连接、权限、异常和 AI 诊断构建自动化回归；当前工作区记录 Python 正式测试 1003 项、前端测试 97 项通过，另有 34 项独立验证测试。
- Python 后端：FastAPI 与应用服务对接，复用 Profile、Runtime 和 Doctor；通过进程身份及 Scope 检查约束操作边界。
- AI 应用：接入 LangChain 工具编排与结构化输出，将 18 个只读诊断工具接入统一 Registry，并对模型结论进行 Evidence 校验。
- 可讲的难点：SSH 半开连接导致旧远端监听残留；模型工具调用越权与跨 Profile 访问；诊断结果引用不存在或跨领域证据；不同 Python / Node 环境的依赖复现。讨论时区分历史实机结论和本次 AI 链路的离线模拟结果。

可以说明用户报告的真实 DeepSeek 隔离模拟数据验证已通过；不要声称新版 LangChain 已在真实服务器端到端通过，除非实际完成对应验收并记录结果。不同测试集的数字应说明范围，避免重复计数。
