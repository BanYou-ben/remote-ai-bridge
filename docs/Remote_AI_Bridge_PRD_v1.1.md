# Remote AI Bridge v1.1

## 1. Product definition

**Remote AI Bridge** is a Windows-to-Linux, user-scoped SSH reverse-proxy bridge. It allows a remote Linux server to reuse an existing local HTTP/Mixed proxy without installing a proxy client, changing server-wide networking, requiring root, or affecting other users.

Primary promise for v1.0:

> Safely bridge an existing local proxy to one remote Linux user and make command-line AI tools usable through it.

The product must not claim that it can repair every Codex Desktop remote-host failure. Desktop support is limited to diagnosis and explicitly confirmed, narrowly scoped runtime recovery.

## 2. Scope and terminology

```text
Remote Linux process
  -> 127.0.0.1:<remote_port>
  -> SSH reverse forwarding
  -> Windows 127.0.0.1:<local_proxy_port>
  -> local HTTP/Mixed proxy
  -> AI service endpoint
```

- **Bridge**: the managed SSH reverse tunnel.
- **CLI environment**: a server shell or script running an AI CLI through the bridge.
- **Desktop remote runtime**: a Codex Desktop-managed process on the remote server. It is separate from an interactive CLI shell.
- **Probe**: a credential-free request used only to verify network reachability.

## 3. Supported environment

| Component | v1.0 support |
|---|---|
| Local OS | Windows 10/11 |
| Remote OS | Ubuntu 22.04+ and compatible Linux shells |
| SSH client | Windows OpenSSH (`ssh.exe`) |
| Local proxy | Loopback HTTP/Mixed proxy without proxy authentication |
| Server access | SSH key or agent authentication; server permits TCP forwarding |
| Primary AI CLI | Codex CLI |

Out of scope: VPN/TUN, proxy subscriptions, proxy-node management, system proxy changes, firewall/iptables edits, root actions, macOS, mobile, multi-user sharing, and model management.

## 4. User outcomes

The product must make these workflows predictable:

1. Select an SSH alias or host.
2. Detect and validate an existing local proxy.
3. Create a loopback-only reverse tunnel.
4. Verify that the remote endpoint reaches OpenAI through the tunnel.
5. Install an opt-in user-level helper for remote CLI use.
6. Diagnose failures with actionable causes rather than treating an SSH process as proof of health.

## 5. Security and privacy requirements

- No root, `/etc` changes, firewall edits, or server-wide proxy settings.
- Reverse forwarding must bind to `127.0.0.1` on the server by default and may never bind to `0.0.0.0` in v1.0.
- Strict SSH host-key verification. A changed host key requires an explicit user decision.
- Do not store ChatGPT tokens, `auth.json`, OpenAI API keys, SSH private-key contents, or proxy subscription data.
- Do not execute SSH through a shell string. Spawn `ssh.exe` with an argument list.
- Redact credentials, authorization headers, tokens, cookies, and proxy credentials in logs and UI.
- Every remote file belongs under `~/.remote-ai-bridge/`, is user-owned, and has a documented uninstall path.
- All mutating actions require an explicit user command or confirmation.

## 6. State model

```text
Disconnected
  -> CheckingLocalProxy
  -> CheckingSSH
  -> StartingTunnel
  -> VerifyingRemotePort
  -> VerifyingEndpoint
  -> Ready

Ready -> Degraded -> Reconnecting -> Ready | Disconnected
```

Health is `Ready` only when all checks pass:

- local proxy accepts a TCP connection;
- proxy answers a valid HTTP proxy request;
- SSH tunnel process is alive and owned by this Bridge instance;
- remote loopback port is listening;
- a credential-free probe reaches the configured AI endpoint through the remote proxy.

An HTTP `401` or `403` from a deliberately unauthenticated OpenAI request indicates **network reachability**, not an authentication failure.

## 7. Phase 1 CLI prototype

### 7.1 Commands

```text
rab profile add <name>
rab profile list
rab connect <name>
rab disconnect <name>
rab status <name>
rab doctor <name>
```

`rab connect` performs detection, starts the tunnel, records its own process identity, then verifies remote endpoint reachability. `rab disconnect` may terminate only the SSH process recorded in that profile and verified as owned by the current Bridge instance.

### 7.2 Profile schema

```json
{
  "schema_version": 1,
  "name": "myserver",
  "ssh_target": "myserver",
  "local_proxy_host": "127.0.0.1",
  "local_proxy_port": 7897,
  "remote_bind_host": "127.0.0.1",
  "remote_port": 17890,
  "auto_reconnect": true,
  "endpoint_probe_url": "https://api.openai.com/v1/models"
}
```

Profiles must not contain private-key content or proxy credentials. Runtime state is stored separately: PID, process creation time, tunnel ID, chosen remote port, and last successful probe timestamp.

### 7.3 Local proxy detection

Candidate ports: `7890`, `7892`, `7897`, `10808`, `10809`, plus user input.

For every candidate, report separately:

```text
TCP reachable       PASS/FAIL
HTTP proxy handshake PASS/FAIL
AI endpoint probe    PASS/FAIL
```

The selected port is never inferred solely from TCP reachability.

### 7.4 SSH tunnel manager

Equivalent managed command:

```text
ssh -N -T
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -R 127.0.0.1:<remote_port>:127.0.0.1:<local_proxy_port>
  <ssh_target>
```

Rules:

- Before creation, inspect the remote port. Reuse only if ownership matches this Bridge instance.
- If an unknown process owns the requested port, do not terminate it. Offer a free port and require confirmation before changing the profile.
- Reconnect with bounded exponential backoff: 1, 2, 5, 10, 30 seconds.
- Every subprocess has timeout, captured stdout/stderr, and a normalized error code.

### 7.5 Remote verification

Use read-only remote commands. Verify the remote listener and issue a credential-free HTTP request through `http://127.0.0.1:<remote_port>`.

Results must distinguish:

| Result | Meaning |
|---|---|
| Listener absent | Tunnel was not created or was lost. |
| Connection refused | Remote proxy port is unavailable. |
| Connection timeout | Tunnel/local proxy/upstream rule is unhealthy. |
| 401/403 from endpoint | Bridge networking is healthy. |
| Successful authenticated response | Bridge networking is healthy. |

## 8. Phase 2: user-level Linux helper

Install only when the user invokes `rab install-helper <name>`.

```text
~/.remote-ai-bridge/
├── config.json
├── env.sh
├── bin/
│   ├── ai-proxy
│   └── rab-codex
├── logs/
└── backup/
```

`ai-proxy` exposes `status`, `test`, and `run <command>`. `rab-codex` exports the loopback proxy variables then executes the canonical real Codex path. It must not be named `codex`, must detect recursive invocation, and must not overwrite a user’s existing Codex executable.

The helper supports CLI shells and scripts. It does not promise to configure Codex Desktop’s independently managed remote runtime.

## 9. Phase 3: Codex CLI doctor

Checks, without reading credentials:

```text
Codex installed
Codex version
Codex login state
Proxy environment presence
Codex no-tools OK probe
```

All CLI probes run through a login shell when appropriate, because non-interactive SSH commands commonly omit npm global PATH and proxy environment variables.

## 10. Phase 4: Desktop remote-runtime doctor

This phase is diagnostic-first and best-effort.

The doctor may identify a potential stale runtime only when it can validate a tuple of:

- current SSH user;
- executable canonical path;
- PID and process creation time;
- parent process chain;
- Codex-specific control/runtime evidence;
- runtime version and presence of required proxy environment.

If identity is ambiguous, do not expose an automatic restart option.

If the user explicitly confirms a restart, the product sends `SIGTERM` only to the validated runtime process, waits for exit, and reports that all active remote Codex tasks may be interrupted. It must never use `pkill -9 codex`, `killall codex`, root, or a broad process-name-only kill.

After termination, it waits for Desktop reconnection and sends a no-tools `OK` probe. A failed automatic reload is reported as a manual-reconnect instruction, not as a silent success.

## 11. GUI boundary

PySide6 GUI begins only after Phases 1–3 pass end-to-end. It has three screens: Bridge, Server Profile, and Doctor. GUI code consumes the same core service interfaces as the CLI and may not contain tunnel logic.

## 12. Phase 1 acceptance tests

- Valid local proxy + valid SSH target reaches `Ready`.
- Local proxy stopped produces a specific local-proxy error.
- Local port reachable but not an HTTP proxy produces a proxy-handshake error.
- SSH transport loss transitions to `Degraded`, then reconnects with bounded backoff.
- Server with TCP forwarding disabled produces a forwarding-specific error.
- Unknown remote-port owner is never terminated.
- A 401/403 unauthenticated endpoint response is reported as network pass.
- Every started tunnel can be disconnected without affecting unrelated SSH processes.
- No log output contains tokens, credentials, authorization headers, or private-key contents.

## 13. Development handoff: Phase 1 only

Implement only the Phase 1 CLI prototype from this PRD. Do not build GUI, Linux helper, Codex wrapper, Desktop runtime restart, packaging, or later phases.

Required deliverables:

1. Project directory tree.
2. Module-level architecture note.
3. Runnable CLI commands.
4. Unit tests using mocked subprocesses.
5. One opt-in Windows-to-Ubuntu integration test plan.
6. Test results, known limitations, and Phase 2 recommendation.

Stop after Phase 1 and wait for explicit user approval.
