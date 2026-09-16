# Phase 1 development handoff

Read `Remote_AI_Bridge_PRD_v1.1.md` before implementation. Build only the CLI prototype.

## Deliverable

Implement a Windows Python 3.10+ package exposing:

```text
rab profile add <name>
rab profile list
rab connect <name>
rab disconnect <name>
rab status <name>
rab doctor <name>
```

## Required modules

```text
app/
  cli.py
  domain/
    profile.py
    health.py
  services/
    local_proxy.py
    ssh_config.py
    tunnel.py
    remote_probe.py
    doctor.py
  infrastructure/
    process_runner.py
    profile_store.py
tests/
```

## Constraints

- Use `ssh.exe` via argument lists, never shell interpolation.
- Every subprocess must have an explicit timeout, captured stdout/stderr, and normalized failure result.
- Build tests first for profile validation, command construction, port-conflict handling, health-state aggregation, and process ownership matching.
- Mock SSH and network subprocesses in unit tests; real SSH use is only an opt-in integration test.
- Do not implement GUI, Linux helper, Codex wrapper, runtime restart, packaging, or future phases.
- Stop after Phase 1 and report the directory tree, commands, test result, limitations, and Phase 2 recommendation.
