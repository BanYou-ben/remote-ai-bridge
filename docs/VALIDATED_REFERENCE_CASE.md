# Validated reference case

This document records a real, successful diagnostic path. It is a development reference, not a hard-coded default.

## Environment

```text
Local OS: Windows 11
Local proxy: loopback HTTP/Mixed proxy at 127.0.0.1:7897
SSH target alias: myserver
Remote OS: Ubuntu 22.04
Remote reverse-proxy endpoint: 127.0.0.1:17890
Codex CLI after update: 0.152.1
```

## Tunnel command

```powershell
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:17890:127.0.0.1:7897 myserver
```

## What passed

1. A TCP probe to local `127.0.0.1:7897` succeeded.
2. The server listened on `127.0.0.1:17890`.
3. A credential-free request sent through remote port `17890` to the OpenAI API returned HTTP `401`; this means the proxy path reached OpenAI and the request merely lacked credentials.
4. In a server login shell, Codex was installed, authenticated through ChatGPT, and a no-tools prompt returned `OK`.

## Important failure mode

The reverse tunnel, proxy, and Codex CLI were healthy while Codex Desktop remote tasks remained `active` without producing output. The cause was a stale server-side Codex remote runtime. Restarting only the confirmed Codex runtime caused existing remote tasks to be interrupted; after Desktop reloaded the remote runtime, a no-tools `OK` probe completed in about four seconds.

## Product implications

- A live SSH process is not a sufficient health signal.
- A CLI wrapper can repair CLI environment inheritance but cannot guarantee configuration of a Desktop-managed remote runtime.
- Runtime termination must be explicit, narrowly scoped, identity-checked, and confirmed by the user.
- Do not encode this host alias or these ports as application defaults. They are an integration fixture only.
