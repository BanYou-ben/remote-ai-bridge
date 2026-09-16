# Opt-in Windows-to-Ubuntu integration test plan

This plan is manual and must never run as part of the unit-test suite.

## Preconditions

- Windows 10/11 with Python 3.10+, OpenSSH `ssh.exe`, and an existing loopback HTTP/Mixed proxy.
- A known-host SSH alias using key or agent authentication.
- An Ubuntu 22.04+ account where TCP forwarding is permitted and `ss` and `curl` are installed.
- No credentials are placed in commands, profiles, captured output, or test artifacts.

## Validated fixture example

The previously validated `myserver`, local port `7897`, and remote port `17890` may be supplied explicitly. They are not application defaults.

```powershell
rab profile add myserver --local-proxy-port 7897 --remote-port 17890
rab doctor myserver
rab connect myserver
```

In a second terminal, run `rab status myserver`. On the server, confirm that only `127.0.0.1:17890` listens. A credential-free OpenAI probe must be reported as a network pass when it returns HTTP 401 or 403.

Press Ctrl+C in the supervising terminal. Confirm that the recorded tunnel exits, `rab status myserver` reports disconnected, and unrelated SSH sessions remain alive.

## Negative cases

1. Stop the local proxy and confirm a specific local TCP/proxy failure.
2. Point a test profile at a plain TCP listener and confirm `NOT_HTTP_PROXY`.
3. Disable forwarding on a dedicated test SSH target and confirm tunnel creation fails without reporting Ready.
4. Occupy the requested remote port with an unrelated listener. Confirm it is not terminated; decline the suggested port and verify the profile is unchanged, then repeat and explicitly accept the new loopback port.
5. Interrupt network transport while connected. Confirm `Degraded`, then reconnect attempts at 1, 2, 5, 10, and at most 30 seconds.
6. Replace or edit the runtime PID identity fields in an isolated state directory. Confirm `status` reports mismatch and `disconnect` does not terminate that PID.
7. Search captured console output and state files for authorization headers, tokens, cookies, private-key material, and proxy credentials; none may be present.

Do not use a shared production server for destructive negative cases. This plan does not test a Linux helper, Codex wrapper, Codex login, Desktop remote runtime, GUI, service installation, startup persistence, or packaging.
