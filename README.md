# Remote AI Bridge — Phase 1 CLI prototype

[English](README.md) | [简体中文](README_zh-CN.md)

Remote AI Bridge creates a user-scoped SSH reverse tunnel from a remote Linux loopback port to an existing Windows loopback HTTP/Mixed proxy. Phase 1 is foreground-only and does not install a service, helper, wrapper, GUI, or Desktop runtime recovery mechanism.

## Requirements

- Windows 10/11
- Python 3.10 or newer
- Windows OpenSSH `ssh.exe`
- SSH key or agent authentication and an already trusted server host key
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

## Local development API

Phase 2.5 includes a local FastAPI runtime control surface. Start it explicitly
on loopback only:

```powershell
python -m uvicorn app.api.app:app --host 127.0.0.1 --port 8000
```

The API currently has no authentication. Do not bind it to `0.0.0.0` or expose
it directly to a LAN or the Internet. `/health` is process liveness only; it
does not probe SSH, the proxy, or remote endpoints.

See `docs/ARCHITECTURE_PHASE_1.md` and `docs/INTEGRATION_TEST_PLAN_PHASE_1.md` for the module boundaries and opt-in real-host test plan.
