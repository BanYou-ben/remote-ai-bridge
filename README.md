# Remote AI Bridge v0.2.0

[English](README.md) | [简体中文](README_zh-CN.md)

Remote AI Bridge creates a user-scoped SSH reverse tunnel from a remote Linux loopback port to an existing Windows loopback HTTP/Mixed proxy. Version 0.2.0 provides CLI and local HTTP surfaces over the same managed setup, diagnostics, profile, and runtime services. It does not install a Windows service or GUI.

Version 0.2.0 includes:

- managed SSH bootstrap with explicit host-key fingerprint confirmation;
- a dedicated per-server RAB key, installed without changing `sshd_config` or using `sudo`;
- local proxy and loopback-only remote-port discovery during managed setup;
- foreground tunnel supervision, bounded reconnect, safe process ownership checks, and stale-session protection;
- persistent managed profiles plus shared CLI and loopback HTTP API access through one service graph.

## Requirements

- Windows 10/11
- Python 3.10 or newer
- Windows OpenSSH `ssh.exe`
- Windows `ssh-keygen` for managed key creation
- For managed bootstrap, password authentication for the current Linux user during initial setup; existing key/agent-based legacy profiles remain supported
- A loopback HTTP/Mixed proxy without proxy authentication
- Remote `ss` and `curl`

Run directly from the repository:

```powershell
.\rab.ps1 profile add myserver --local-proxy-port 7897 --remote-port 17890
.\rab.ps1 profile list
.\rab.ps1 doctor myserver
.\rab.ps1 connect myserver
```

If Python is not discoverable, point the launcher at a Python 3.10+ executable:

```powershell
$env:RAB_PYTHON = "C:\path\to\python.exe"
```

`connect` remains in the foreground. While alive, it probes bridge health and reconnects using delays of 1, 2, 5, 10, then at most 30 seconds. Press Ctrl+C to stop supervision and its identity-verified tunnel.

`status` only reads the saved runtime state and validates the recorded PID, process creation time, and executable path. It does not perform network probes, reconnect, or mutate state.

Only one `connect` supervisor may run for a profile. While it is active, a separate `disconnect` command refuses the operation because the supervisor would otherwise reconnect; use Ctrl+C in the supervising terminal.

Profiles and runtime state are stored separately under `%LOCALAPPDATA%\RemoteAIBridge`. Profile files contain no private keys or proxy credentials.

## Development checks

```powershell
python -m pytest
python -m app.cli --help
```

## Local API server

Start the v0.2.0 backend with the supported single-process entry point:

```powershell
rab serve
rab serve --port 8000
rab --state-dir C:\path\to\state serve
```

The default URL is `http://127.0.0.1:8000`. The API currently has no
authentication, so `rab serve` accepts loopback hosts only and rejects LAN,
public, and `0.0.0.0` bindings. Do not expose it to a LAN or the Internet.
Multi-worker and auto-reload modes are intentionally unsupported because the
RuntimeManager is process-local. `/health` is process liveness only; it does
not probe SSH, the proxy, or remote endpoints.

The local API also provides profile inspection and safe mutation under
`/profiles`, managed setup steps under `/setup/host/*`,
`/setup/local-proxy/discover`, and `/setup/managed`, plus active diagnostics at
`POST /doctor/{name}`. Managed setup accepts an SSH password only for the
in-process bootstrap call. The password is not persisted, returned, or logged;
Python immutable strings cannot be reliably erased from memory.

The v0.2.0 backend uses one shared service graph:

```text
CLI / HTTP API
       |
   AppServices
       |
ProfileService / SetupService / RuntimeManager / DoctorService
       |
Infrastructure
```

See `docs/ARCHITECTURE_PHASE_1.md` and `docs/INTEGRATION_TEST_PLAN_PHASE_1.md` for the module boundaries and opt-in real-host test plan.

## Web development

Phase 4.1 provides a development-only, read-only Vue dashboard. Start the backend first:

```powershell
rab serve
```

Based on the current locked development and test dependencies, the frontend requires Node.js `^22.22.2`, `^24.15.0`, or `>=26.0.0`. The currently verified environment is Node.js `24.19.0`; Node.js 22 and 26 have not been tested. For a fresh checkout, use the committed lockfile for a clean, reproducible install:

```powershell
cd web
npm ci
npm run dev
```

Use `npm install` when intentionally updating dependencies or the lockfile.

Open `http://127.0.0.1:5173`. The browser sends `/api` requests to the Vite development server, which proxies them to `http://127.0.0.1:8000`; FastAPI CORS does not need to be enabled. This is not yet a production Web release.
