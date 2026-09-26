# Remote AI Bridge v0.4.0

[English](README.md) | [简体中文](README_zh-CN.md)

Remote AI Bridge (RAB) is a Windows-local SSH reverse-tunnel manager and network diagnostic tool. It connects an existing Windows loopback HTTP/Mixed proxy to a loopback endpoint on a remote Linux server, while handling setup, tunnel supervision, profile management, and layered diagnostics.

```text
Windows loopback HTTP/Mixed proxy
                 |
        Remote AI Bridge
                 |
        SSH reverse tunnel
                 |
Remote Linux loopback endpoint
```

RAB removes the need to hand-write routine `ssh -R` commands or diagnose proxy, SSH, remote-port, and runtime failures as unrelated problems. It is not a VPN, a general NAT-traversal platform, or a cloud service. Version 0.4.0 targets a Windows local host and does not include an AI diagnostic assistant; that remains future v0.5 work.

## Features

### Managed Setup

The Web setup wizard and the shared backend support:

- server host, username, and SSH port input;
- SSH Host Key discovery before password authentication;
- explicit user confirmation of the displayed Host Key fingerprint;
- Windows loopback proxy discovery and health validation;
- one-time SSH password authentication for bootstrap;
- a dedicated RAB SSH key and user-level `authorized_keys` installation;
- loopback-only remote-port selection;
- persistent Managed Profile creation.

The SSH password is used only for the bootstrap request. It is not persisted, returned by the API, or intentionally logged. The Web form clears its temporary password state after either success or failure. Python strings are immutable, so RAB does not claim that password bytes can be securely erased from process memory.

### Profiles

- Managed Profiles created by the setup workflow;
- legacy profiles for existing SSH configuration or key/agent-based access;
- list and detailed inspection through CLI, API, and Web UI;
- safe updates for the fields accepted by the backend;
- ownership-aware legacy deletion;
- a deliberate safety boundary that refuses Managed Profile deletion until managed credential revocation is implemented.

### Runtime management

- connect and disconnect operations;
- runtime state inspection and foreground supervision;
- bounded reconnect backoff;
- process identity and ownership validation;
- preservation of unresolved runtime or remote-session ownership evidence;
- protection against killing unknown local or remote processes.

Runtime states include `STARTING`, `CONNECTING`, `READY`, `DEGRADED`, `FAILED`, `STOPPING`, `STOPPED`, and `UNSUPERVISED`.

### Doctor

Doctor reports separate checks for:

- local proxy TCP reachability;
- HTTP proxy handshake;
- external endpoint reachability through the proxy;
- SSH configuration resolution;
- the owned tunnel process;
- the remote loopback listener;
- the remote endpoint through the bridge.

### Web UI

The v0.4.0 Vue Web UI provides:

- Dashboard status and runtime controls;
- connection/Profile listing;
- Managed Setup Wizard;
- Profile details and safe updates;
- Connect and Disconnect actions with bounded action-scoped polling;
- Doctor results;
- structured loading, empty, success, status, and error states.

The current repository runs the Web UI with Vite during source development. FastAPI does not serve the built frontend as a single production distribution yet.

## Before you start

Prepare the following:

- Windows 10 or Windows 11;
- Python 3.10 or newer;
- Windows OpenSSH `ssh.exe` and `ssh-keygen`;
- an HTTP/Mixed proxy listening on Windows loopback, for example `127.0.0.1:7897`, without proxy authentication;
- a Linux server reachable over SSH and an account you are authorized to use;
- `ss` and `curl` available for that Linux user;
- Node.js `^22.22.2`, `^24.15.0`, or `>=26.0.0` only when running the current Web UI from source.

The first Managed Setup requires password authentication to be available for the selected Linux user. RAB uses that password only to install the dedicated public key and verify key-based login. Existing legacy profiles may instead use a preconfigured SSH target, key, or agent.

You do not need to create an `ssh -R` tunnel before opening the Web UI. RAB performs Host Key verification, managed bootstrap, and later tunnel management. The reverse tunnel still requires working SSH connectivity between Windows and the Linux server.

## Install and run from source

Clone the repository, open PowerShell in the project directory, and install the Python package:

```powershell
python -m pip install -e .
```

Start the local backend:

```powershell
rab serve
```

The backend listens on `http://127.0.0.1:8000` by default. In another terminal, install and start the Web development server:

```powershell
cd web
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies browser requests from `/api` to `http://127.0.0.1:8000`.

Use `npm install` only when intentionally changing frontend dependencies or the lockfile. This release does not provide a Windows installer, background Windows service, or combined production Web executable.

## First connection

1. Start the backend with `rab serve`.
2. Start the Web UI with `npm run dev` from `web/`.
3. Open **Add Connection**.
4. Enter a server such as `example.test`, user `alice`, and its SSH port.
5. Compare the displayed SSH Host Key fingerprint through a trusted channel, then explicitly confirm it.
6. Run local proxy discovery and select a healthy candidate such as `127.0.0.1:7897`.
7. Enter the SSH password for the one-time bootstrap.
8. Create the Managed Profile. RAB selects a loopback remote port such as `127.0.0.1:17890`.
9. Return to Dashboard and select **Connect**.
10. Run **Doctor** if a layer does not become healthy.
11. Select **Disconnect** when finished.

## CLI

The CLI and Web UI use the same application services and state directory. Common commands include:

```powershell
rab profile list
rab profile get <name>
rab status <name>
rab doctor <name>
rab connect <name>
rab disconnect <name>
```

`rab connect` runs foreground supervision. Ctrl+C requests an ownership-aware stop of the supervisor and tunnel. Runtime and Profile JSON are stored separately under `%LOCALAPPDATA%\RemoteAIBridge` unless `--state-dir` is provided.

## Security

- The FastAPI server currently has no authentication and therefore accepts loopback bind addresses only. Never expose it to a LAN or the Internet.
- Unknown SSH Host Keys require explicit fingerprint confirmation before password authentication. A changed Host Key is not automatically accepted.
- Managed Setup passwords are not persisted, returned, or intentionally logged.
- The Web UI does not display passwords or private-key material.
- Local proxy discovery and remote forwarding are restricted to loopback addresses; the remote listener defaults to `127.0.0.1`.
- Process identity, creation time, executable path, runtime ownership, and saved remote-session identity protect stop and stale-session cleanup operations.
- RAB does not terminate a process merely because it owns a configured port.
- Managed Profile deletion is refused while safe remote credential revocation is unavailable. The UI reports this boundary instead of pretending deletion succeeded.
- `RuntimeManager` is process-local. `rab serve` runs a single worker without auto-reload.

## Architecture

```text
Web UI / CLI
      |
FastAPI / AppServices
      |
ProfileService   SetupService   RuntimeManager   DoctorService
      |
SSH / Proxy / Filesystem infrastructure
```

The CLI and HTTP API share the same service graph; frontend code does not reimplement SSH, Host Key, proxy, process-ownership, or credential safety logic.

## Development and verification

Python checks:

```powershell
python -m pytest
python -m compileall app tests tools
```

Frontend checks:

```powershell
cd web
npm ci
npm test -- --run
npm run build
```

See [CHANGELOG.md](CHANGELOG.md) for release changes. Phase 1 architecture and real-host test references remain in [`docs/`](docs/).
