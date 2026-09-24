# Remote AI Bridge — Phase 1 CLI 原型

[English](README.md) | [简体中文](README_zh-CN.md)

Remote AI Bridge 通过用户级 SSH 反向隧道，将 Linux 远端服务器的本地回环端口转发到 Windows 上现有的本地 HTTP/Mixed 代理。Phase 1 仅以前台进程运行，不安装 Windows 服务、Linux helper、命令包装器、GUI，也不负责恢复 Codex Desktop 远端运行时。

## 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- Windows OpenSSH `ssh.exe`
- SSH 密钥或 SSH Agent 认证，并且服务器 Host Key 已受信任
- 无代理认证的本地回环 HTTP/Mixed 代理
- Linux 远端服务器已安装 `ss` 和 `curl`

直接在项目目录中运行：

```powershell
.\rab.ps1 profile add myserver --local-proxy-port 7897 --remote-port 17890
.\rab.ps1 profile list
.\rab.ps1 doctor myserver
.\rab.ps1 connect myserver
```

如果启动脚本无法找到 Python，可以通过环境变量指定 Python 3.10 及以上版本的解释器：

```powershell
$env:RAB_PYTHON = "C:\path\to\python.exe"
```

`connect` 会一直在前台运行。在进程存活期间，它会持续检查桥接健康状态，并按照 1、2、5、10、最高30秒的有界指数退避进行恢复。按 Ctrl+C 会停止前台监督，并停止经过进程身份校验的 SSH Tunnel。

`status` 只读取已保存的 runtime state，并检查记录的 PID、进程创建时间和可执行文件路径。它不会执行网络探测、自动重连或修改运行时状态。

同一个 profile 同时只允许存在一个 `connect` 前台监督进程。监督进程运行期间，单独执行 `disconnect` 会被拒绝，因为监督进程可能立即重新建立连接；此时应在运行 `connect` 的终端中按 Ctrl+C。

Profile 和 runtime state 分别保存在 `%LOCALAPPDATA%\RemoteAIBridge` 下。Profile 文件不会保存 SSH 私钥、代理凭据、API Key 或 Token。

## 开发验证

```powershell
python -m pytest
python -m app.cli --help
```

## 本地开发 API

Phase 2.5 提供仅用于本地的 FastAPI Runtime 控制接口。请明确绑定本机回环地址启动：

```powershell
python -m uvicorn app.api.app:app --host 127.0.0.1 --port 8000
```

当前 API 尚未实现身份认证。不要绑定 `0.0.0.0`，也不要直接暴露到局域网或互联网。
`/health` 只表示 API 进程存活，不会探测 SSH、本地代理或远端 Endpoint。

本地 API 还提供 `/profiles` 下的 Profile 查询与安全修改、`/setup/host/*`、
`/setup/local-proxy/discover`、`/setup/managed` 管理式配置流程，以及主动诊断接口
`POST /doctor/{name}`。Managed Setup 接收的 SSH password 只用于当前进程内的 Bootstrap
调用，不会持久化、返回或记录；Python 不可变字符串无法保证从内存中可靠擦除。

模块边界和需要人工启用的真实服务器测试方案，请参阅：

- [`docs/ARCHITECTURE_PHASE_1.md`](docs/ARCHITECTURE_PHASE_1.md)
- [`docs/INTEGRATION_TEST_PLAN_PHASE_1.md`](docs/INTEGRATION_TEST_PLAN_PHASE_1.md)
