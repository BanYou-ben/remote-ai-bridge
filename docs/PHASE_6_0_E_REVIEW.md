# Phase 6.0-E：真实 Linux Agent 联调准备

## 当前状态

- 基线：`feature/v0.5-ai-diagnostics`，`5dc785afdf35170f7191ac5afdec5f8f375b073d`。
- 本阶段仅代码检查和离线测试：未连接真实服务器、未调用付费 API、未读取真实 API Key。
- Phase 6.0-D 用户报告真实 DeepSeek API + 内存模拟工具多轮验证成功；**真实 Linux、真实 Profile 和真实诊断工具的 Agent 端到端尚未验证**。
- 新增 `tests/test_phase60e_agent_integration.py`，使用生产 registry 和 AgentRunner、模拟依赖/模型，验证 SSH TCP 故障与 Doctor Evidence、多轮最终 grounding、工具异常、模型超时、Evidence 不足及不调用 start/stop/disconnect。
- 定向测试 49 passed；完整正式 pytest 982 passed；两个 validation 入口测试 26 passed。以上均为离线结果，不能替代实机验收。

## 18 个工具：调用链及权限

Registry 通过 `create_services()` 装配，全部声明为 `read_only`。AgentRunner 在暴露和执行时检查 risk，并检查 `profile_name`；工具参数严格验证。`ok=true` 表示工具成功返回，不等于其 Evidence 中的健康状态为 PASS。

| 工具 | 实际调用和影响 |
| --- | --- |
| `get_runtime_status` | RuntimeManager.status：读取本进程 supervisor snapshot 或 runtime 文件，校验本地进程身份；不启动隧道。 |
| `get_profile_summary` | 读取 Profile 并返回摘要；包含 endpoint 等配置，可能外发。 |
| `run_doctor` | Doctor：本地代理检查、SSH 配置检查、owned tunnel 进程检查；满足条件时再检查远端 listener 与 endpoint。 |
| `check_local_proxy` | 本机 TCP、HTTP CONNECT 与通过代理向已配置 endpoint 发 GET；会产生实际网络流量。 |
| `check_ssh_config` | 本机 `ssh -G` 解析配置，不建立 SSH 登录。 |
| `check_ssh_transport` | managed Profile 的 host/port DNS 和 TCP；legacy 无 host/port 时为 SKIP。 |
| `check_tunnel_process` | 读取 Runtime、校验本地 owned SSH 进程身份。 |
| `check_remote_listener` | 新建受控 SSH 会话，在远端只读执行固定 `ss -ltnH`。 |
| `check_remote_endpoint` | 新建受控 SSH 会话，通过远端 loopback 反向代理对配置 endpoint 执行 curl GET。 |
| `get_remote_system_info` | 受控 SSH 读取 `/etc/os-release`、kernel、architecture。 |
| `check_remote_memory` | 受控 SSH 读取 `/proc/meminfo`。 |
| `check_remote_disk` | 受控 SSH 执行固定 `df -Pk -- /`。 |
| `check_remote_load` | 受控 SSH 读取 `/proc/loadavg` 和 CPU 数量。 |
| `get_remote_top_processes` | 受控 SSH 读取最多五个进程名及 CPU/内存占比。 |
| `check_remote_failed_services` | 受控 SSH 使用固定 systemctl 命令查询失败服务；非 systemd 等可能 SKIP。 |
| `list_application_targets` | 从本地 store 列出绑定当前 Profile 的 trusted target 名和能力。 |
| `check_application_service` | 校验 Target/Profile 绑定，经受控 SSH 查询已配置 unit 的 systemctl 状态。 |
| `check_application_health` | 校验绑定，经受控 SSH 对已配置的远端 loopback URL 发 curl GET；不走代理、不跟随重定向。 |

上述 SSH 新会话使用 `RemoteProbeService.ssh_prefix()` 的 BatchMode、严格 Host Key 和既有身份配置，ProcessRunner 使用 `shell=False`；远端执行的是代码内固定诊断命令，Agent 不提供 shell、命令、URL 或服务名。无 sudo、kill、restart 或配置修改。虽属只读诊断，TCP/SSH/HTTP 请求仍会被服务端记录并消耗资源；即使 GET 也可能触发服务自身副作用，endpoint 必须由用户确认可安全探测。

## 失败与降级

- ToolRegistry 将 handler 异常转为 `TOOL_EXECUTION_FAILED`，AgentRunner 可继续下一轮；工具内的 FAIL/SKIP 则作为 Evidence 保留。SSH 不可达、没有 runtime 或身份不匹配应按真实结果报告，不能推断远端状态。
- 模型超时由 Adapter 映射为 `AGENT_MODEL_TIMEOUT`；不足证据或跨领域引用由 Runner 拒绝。多轮与故障路径已有 fake model、HTTP mock 和本次 registry 离线测试，但真实模型选择与输出仍未验证。
- Agent 模型不可用不会由此自动停止 SSH/Runtime；现有 `rab doctor <profile>` 是独立手动入口。Doctor 自身仍会执行前述只读网络探测，因此使用真实 Profile 前也需要授权。
- 当前入口的 AgentRunner 在独立进程创建；它不会共享另一个进程中的内存 supervisor worker。存在 runtime 时可验证进程身份，但可能显示 UNSUPERVISED；不能把该字段直接等同于另一个进程中的监督状态。

## 真实联调前提和明确授权范围

1. 用户指定并授权**一个测试 Profile 与对应 Linux 主机/账号**；确认该 Profile 使用的 SSH 用户可通过现有 Windows OpenSSH BatchMode 登录，Host Key 已可信，权限能读取相关 `/proc`、`/etc/os-release` 并运行 `ss`、`curl`、`df`、`ps`、`getconf`、`systemctl`（systemd 若不可用则记录 SKIP）。不要求 sudo。
2. 用户自行保证本地代理与已有 Tunnel 状态；本测试不启动、停止或修改 Tunnel。要验证断连/故障，只使用用户事先提供的隔离测试状态，不主动断网、kill、重启或修改配置。
3. 确认 Profile 的 endpoint 以及任何 application target 的 GET 可安全执行。Agent 可自主选择 18 个注册工具，不能保证只调用问题中点名的工具；若不接受其他工具或额外 GET，当前入口尚无工具白名单，**不应开始实测**。
4. 用户明确同意诊断问题、工具 observations 和 Evidence 发送至 DeepSeek。它们可能包括 Profile/Target 名、SSH target、端口、PID、进程/服务名、网络诊断 detail、endpoint 信息或主机资源信息。现有 redaction 无法保证全部隐私信息去除；不要将真实敏感结果直接复制到公开报告。
5. 用户本机安全提供 API Key，不发送到聊天、命令行参数或报告。已暴露的旧 Key 应保持撤销。

## 用户授权后的运行方法

由用户在 Windows 的 BY 环境、项目根目录执行，替换 `<测试Profile名>`：

```powershell
D:\anaconda\envs\BY\python.exe -m tools.validation.agent_model_validation --profile <测试Profile名> --question "请检查 SSH、隧道、代理和 Linux 资源状态，并基于证据诊断" --max-steps 6
```

入口显示模型、费用和数据外发提示；用户同意后输入大写 `YES`，再通过本地隐藏输入提供 Key（或事先自行配置 `DEEPSEEK_API_KEY`）。不要把密钥粘贴进问题或命令参数。

`max_steps=6` 包含最终诊断那一轮，最多五次工具决策后提交诊断；每次模型决定调用一次 Responses API，现有 Adapter 的 SDK `max_retries=1`，所以最多约 **6 个逻辑请求、12 次 HTTP 尝试**（重试仅在 SDK 判断可重试时发生），实际次数和费用需以供应商记录为准。各工具本身可能再发 SSH/TCP/HTTP 请求。模型可能重复选择工具或提前结束，不能承诺一次覆盖所有类别。

记录安全结果：确认工具名称、`status`、最终 `diagnosis.stage` 和 Evidence ID。当前入口不展示完整 Evidence 内容，无法单靠它逐条人工核验每项 PASS/FAIL；若需审查详细结果，应先设计安全展示或由用户在授权范围内独立运行 Doctor。不要将 `tools[].ok` 误读为诊断 PASS。若 Agent 不可用，用户可单独执行 `rab doctor <测试Profile名>`，但这同样是实际网络访问。

## 推荐实机验收顺序（尚未执行）

1. 确认授权和外发范围，检查 Profile/现有隧道的测试前状态；用户明确同意必要网络探测。
2. 在已有正常测试状态下运行 Agent，观察 SSH 配置/传输、Tunnel、代理/remote listener、Linux 资源与最终 Evidence grounding。若一次未覆盖，另外授权并记录下一次调用和费用。
3. 在用户事先准备的隔离故障状态下测试 SSH 不可达、无 Tunnel、代理不可用或 Evidence 不足；不由本测试破坏现有连接。
4. 以已有离线 fake 测试覆盖 API timeout；真实超时/服务不可用只在自然出现或用户明确批准的隔离条件下观察，验证手动 Doctor 可用。
5. 校验结果与独立只读检查一致，记录只含非敏感类别的 PASS/FAIL；不显示 Key、原始 stderr、完整远端日志或隐私数据。

## 尚需解决／确认

- 真实测试 Profile、主机和 API 调用尚无本次明确授权，且用户尚未确认对 18 工具及其数据外发的接受范围。
- Phase 5.9 application diagnostics 在真实 Linux 上尚未验证；systemctl 非零退出原因可能被归为管理器不可用。
- 上述脱敏与展示不足以保证所有环境数据不外发；入口也不提供限制单次 Agent 可见工具的参数。
- 尚未执行真实 Linux Agent、多轮故障诊断或真实 API timeout；本报告不声称这些测试通过。
