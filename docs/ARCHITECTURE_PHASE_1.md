# Phase 1 module architecture

The CLI is a thin composition layer. It owns user interaction and the foreground supervision loop but delegates validation, probing, process execution, and persistence.

```text
app.cli
  -> domain.profile / domain.health
  -> services.local_proxy
  -> services.ssh_config
  -> services.tunnel
       -> services.remote_probe
       -> infrastructure.process_runner
       -> infrastructure.process_identity
       -> infrastructure.profile_store
  -> services.doctor
```

## Boundaries

- `domain`: validated, dependency-free profile, runtime-state, and health types.
- `services`: local proxy checks, SSH command construction, remote read-only probes, tunnel lifecycle, and Phase 1 bridge diagnostics.
- `infrastructure`: bounded subprocess results, Windows process identity, and atomic JSON persistence.
- `cli`: commands, redacted presentation, explicit port-change confirmation, and in-process reconnect supervision.

`rab connect` is deliberately foreground-only. Reconnect behavior exists only while that command remains alive. `Ctrl+C` stops supervision and terminates only a tunnel whose PID, creation time, and executable path match the recorded runtime state. `rab status` does not probe or mutate; it reads runtime state and checks that same process identity tuple.

An operating-system file lock permits only one foreground supervisor per profile and is released automatically if that process exits. While the lock is held, `rab disconnect` refuses to create a reconnect loop and directs the user to stop the foreground supervisor with Ctrl+C.

Profiles and runtime state are separate JSON files below `%LOCALAPPDATA%\RemoteAIBridge` by default. Tests can redirect the root with the hidden `--state-dir` development option.
