# Remote AI Bridge v0.2.0

[English](README.md) | [简体中文](README_zh-CN.md)

Remote AI Bridge 通过用户级 SSH 反向隧道，将 Linux 远端服务器的本地回环端口转发到 Windows 上现有的本地 HTTP/Mixed 代理。v0.2.0 通过同一套 Service Layer 同时提供 CLI、本地 HTTP API、Managed Setup、诊断、Profile 和 Runtime 管理，但不安装 Windows 服务或 GUI。

v0.2.0 包含：

- 需要用户明确确认 Host Key Fingerprint 的 Managed SSH Bootstrap；
- 每台服务器独立的 RAB Key，安装过程不修改 `sshd_config`、不使用 `sudo`；
- Managed Setup 阶段的本地代理发现和远端回环端口发现；
- 前台 Tunnel 监督、有界重连、严格进程身份校验和远端残留会话保护；
- Managed Profile 持久化，以及通过同一 Service Graph 提供的 CLI 与本地回环 HTTP API。

## 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- Windows OpenSSH `ssh.exe`
- Windows `ssh-keygen`，用于生成 Managed SSH Key
- 使用 Managed Bootstrap 时，Linux 当前用户需要在初次配置期间支持密码认证；已有的密钥/SSH Agent Legacy Profile 仍受支持
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

## 本地 API Server

使用正式的单进程入口启动 v0.2.0 后端：

```powershell
rab serve
rab serve --port 8000
rab --state-dir C:\path\to\state serve
```

默认地址为 `http://127.0.0.1:8000`。当前 API 尚未实现身份认证，因此 `rab serve`
只接受本机回环地址，并拒绝局域网、公网和 `0.0.0.0`。不要将其暴露到局域网或互联网。
RuntimeManager 是进程内对象，所以明确不支持 multi-worker 和自动 reload。
`/health` 只表示 API 进程存活，不会探测 SSH、本地代理或远端 Endpoint。

本地 API 还提供 `/profiles` 下的 Profile 查询与安全修改、`/setup/host/*`、
`/setup/local-proxy/discover`、`/setup/managed` 管理式配置流程，以及主动诊断接口
`POST /doctor/{name}`。Managed Setup 接收的 SSH password 只用于当前进程内的 Bootstrap
调用，不会持久化、返回或记录；Python 不可变字符串无法保证从内存中可靠擦除。

v0.2.0 后端共享同一个服务图：

```text
CLI / HTTP API
       |
   AppServices
       |
ProfileService / SetupService / RuntimeManager / DoctorService
       |
Infrastructure
```

模块边界和需要人工启用的真实服务器测试方案，请参阅：

- [`docs/ARCHITECTURE_PHASE_1.md`](docs/ARCHITECTURE_PHASE_1.md)
- [`docs/INTEGRATION_TEST_PLAN_PHASE_1.md`](docs/INTEGRATION_TEST_PLAN_PHASE_1.md)
