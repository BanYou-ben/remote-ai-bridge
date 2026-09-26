# Remote AI Bridge v0.4.0

[English](README.md) | [简体中文](README_zh-CN.md)

Remote AI Bridge（RAB）是一个运行在 Windows 本地的 SSH 反向隧道管理与网络故障诊断工具。它将 Windows 回环地址上已有的 HTTP/Mixed 代理连接到 Linux 远端服务器的回环端点，并统一处理配置、Tunnel 监督、Profile 管理和分层诊断。

```text
Windows 本地回环 HTTP/Mixed Proxy
                 |
        Remote AI Bridge
                 |
          SSH Reverse Tunnel
                 |
Linux 远端回环 Endpoint
```

RAB 让用户不必手写常规 `ssh -R` 命令，也不必把 Proxy、SSH、Remote Port 和 Runtime 故障割裂排查。它不是 VPN、通用 NAT 穿透平台或云服务。v0.4.0 面向 Windows 本地主机，当前不包含 AI Diagnostic Assistant；该能力属于后续 v0.5 工作。

## 功能

### Managed Setup

Web 设置向导与共享后端支持：

- 输入服务器地址、用户名和 SSH 端口；
- 在密码认证前读取 SSH Host Key；
- 由用户明确确认页面展示的 Host Key Fingerprint；
- 发现并验证 Windows 本地回环代理；
- 使用一次性 SSH 密码完成 Bootstrap；
- 创建独立的 RAB SSH Key，并将公钥安装到当前 Linux 用户的 `authorized_keys`；
- 选择仅绑定回环地址的远端端口；
- 持久化 Managed Profile。

SSH 密码只用于当前 Bootstrap 请求，不会被持久化、通过 API 返回或主动写入日志。无论成功或失败，Web 表单都会清空临时密码状态。由于 Python 字符串不可变，RAB 不宣称能够从进程内存中安全擦除密码字节。

### Profile

- 由设置流程创建的 Managed Profile；
- 使用已有 SSH 配置、密钥或 Agent 的 Legacy Profile；
- 通过 CLI、API 和 Web UI 查询列表与详情；
- 仅修改后端明确允许的安全字段；
- Legacy Profile 的所有权感知删除；
- 在尚未实现 Managed Credential 撤销时，明确拒绝删除 Managed Profile。

### Runtime 管理

- Connect 与 Disconnect；
- Runtime 状态查询和前台监督；
- 有界重连退避；
- 进程身份与所有权校验；
- 在远端会话状态未解决时保留 Runtime ownership evidence；
- 禁止误杀未知本地或远端进程。

Runtime 状态包括 `STARTING`、`CONNECTING`、`READY`、`DEGRADED`、`FAILED`、`STOPPING`、`STOPPED` 和 `UNSUPERVISED`。

### Doctor

Doctor 分别检查：

- 本地代理 TCP 可达性；
- HTTP Proxy Handshake；
- 通过代理访问外部 Endpoint；
- SSH 配置解析；
- owned Tunnel 进程；
- Linux 远端回环 Listener；
- 通过 Bridge 访问远端 Endpoint。

### Web UI

v0.4.0 Vue Web UI 支持：

- Dashboard 状态展示与 Runtime 操作；
- 连接配置/Profile 列表；
- Managed Setup Wizard；
- Profile 详情与安全更新；
- Connect、Disconnect 与有界 action-scoped polling；
- Doctor 诊断结果；
- 结构化 Loading、Empty、Success、Status 和 Error 状态。

当前仓库通过 Vite 从源码运行 Web UI。FastAPI 尚未把构建后的前端作为单进程生产发行包提供。

## 使用前准备

需要准备：

- Windows 10 或 Windows 11；
- Python 3.10 或更高版本；
- Windows OpenSSH `ssh.exe` 和 `ssh-keygen`；
- 一个监听 Windows 回环地址的 HTTP/Mixed Proxy，例如 `127.0.0.1:7897`，且不要求代理认证；
- 一台可通过 SSH 访问、且你有权使用的 Linux 服务器；
- 当前 Linux 用户环境中可用的 `ss` 和 `curl`；
- 仅在从源码运行当前 Web UI 时需要 Node.js `^22.22.2`、`^24.15.0` 或 `>=26.0.0`。

首次 Managed Setup 要求目标 Linux 用户能够进行密码认证。RAB 只用该密码安装独立公钥并验证密钥登录。已有 Legacy Profile 可以继续使用预配置的 SSH Target、密钥或 Agent。

打开 Web UI 前不需要手动建立 `ssh -R`。RAB 负责 Host Key 验证、Managed Bootstrap 和后续 Tunnel 管理，但 Reverse Tunnel 仍然依赖 Windows 与 Linux 服务器之间的 SSH 可达性。

## 从源码安装和启动

克隆仓库后，在项目目录打开 PowerShell，安装 Python 包：

```powershell
python -m pip install -e .
```

启动本地后端：

```powershell
rab serve
```

后端默认监听 `http://127.0.0.1:8000`。在另一个终端安装并启动 Web 开发服务器：

```powershell
cd web
npm ci
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。Vite 会把浏览器的 `/api` 请求代理到 `http://127.0.0.1:8000`。

只有在明确修改前端依赖或 lockfile 时才使用 `npm install`。当前版本不提供 Windows 安装程序、后台 Windows Service 或前后端合并的生产可执行文件。

## 第一次连接

1. 使用 `rab serve` 启动后端。
2. 在 `web/` 中使用 `npm run dev` 启动 Web UI。
3. 打开“添加连接”。
4. 输入服务器 `example.test`、用户 `alice` 和 SSH 端口。
5. 通过可信渠道核对页面展示的 SSH Host Key Fingerprint，并明确点击确认。
6. 检测本地代理，并选择健康候选，例如 `127.0.0.1:7897`。
7. 输入仅用于本次 Bootstrap 的 SSH 密码。
8. 创建 Managed Profile；RAB 会选择类似 `127.0.0.1:17890` 的回环远端端口。
9. 返回总览并点击“连接”。
10. 如有层级未通过，运行“诊断”。
11. 使用结束后点击“断开”。

## CLI

CLI 和 Web UI 使用同一套应用服务与状态目录。常用命令：

```powershell
rab profile list
rab profile get <name>
rab status <name>
rab doctor <name>
rab connect <name>
rab disconnect <name>
```

`rab connect` 在前台执行监督。Ctrl+C 会请求以所有权校验方式停止 Supervisor 和 Tunnel。除非指定 `--state-dir`，Runtime 与 Profile JSON 会分别保存在 `%LOCALAPPDATA%\RemoteAIBridge`。

## 安全说明

- FastAPI 当前没有身份认证，因此只接受回环地址绑定。禁止暴露到局域网或互联网。
- 未知 SSH Host Key 必须由用户明确确认 Fingerprint 后才能进行密码认证；Host Key 变化不会被自动接受。
- Managed Setup 密码不会持久化、返回或主动写入日志。
- Web UI 不展示密码或 Private Key 内容。
- 本地代理发现和远端转发均限制在回环地址；远端 Listener 默认绑定 `127.0.0.1`。
- 停止进程和清理残留会话前会校验进程身份、创建时间、可执行路径、Runtime ownership 和已保存的远端会话身份。
- RAB 不会仅凭端口号终止进程。
- 在无法安全撤销远端 Managed Credential 时，Managed Profile 删除会被拒绝；Web UI 会展示该边界，而不是伪装删除成功。
- `RuntimeManager` 是进程内对象；`rab serve` 使用单 worker 且不启用自动 reload。

## 架构

```text
Web UI / CLI
      |
FastAPI / AppServices
      |
ProfileService   SetupService   RuntimeManager   DoctorService
      |
SSH / Proxy / Filesystem infrastructure
```

CLI 与 HTTP API 共用同一个 Service Graph。前端不会重新实现 SSH、Host Key、代理、进程所有权或 Credential 安全逻辑。

## 开发与验证

Python：

```powershell
python -m pytest
python -m compileall app tests tools
```

Frontend：

```powershell
cd web
npm ci
npm test -- --run
npm run build
```

版本变化见 [CHANGELOG.md](CHANGELOG.md)。Phase 1 架构与真实服务器测试参考保留在 [`docs/`](docs/) 中。
