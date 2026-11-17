# Phase 5.9 Review Report

Phase 5.9 adds read-only application diagnostics for trusted application targets. The application tools are scoped to the requested profile and use the existing SSH execution and diagnostic evidence infrastructure.

## Review findings and fixes

- A curl timeout reported as exit code 28 was previously classified as an unreachable endpoint. It is now classified as `APPLICATION_HEALTH_TIMEOUT`.
- Curl URL globbing could turn a single configured health URL into multiple requests. The health command now includes `--globoff`.
- A persisted target whose internal name did not match the requested filename could produce evidence under a different target name. The tool now rejects the mismatch before profile lookup or remote execution.

Regression tests cover each fix, including a real persisted target with a mismatched name.

## Security and behavior

- The service check uses a fixed `systemctl show` command with a validated, quoted service unit.
- The health check uses a validated loopback URL, disables proxy use and curl configuration, does not follow redirects, and discards the response body.
- The AgentRunner enforces request profile scope. The application tool independently checks that the target belongs to the requested profile.
- Evidence IDs include the target name and diagnostic type. The registry contains 18 read-only tools, including three application tools.
- Tool results do not intentionally expose raw command output, stderr, credentials, or health response bodies.

## Verification

- Targeted tests: **71 passed**.
- Full Python regression: **953 passed**.
- `git diff --check`: passed; Git reported LF/CRLF warnings.
- The full suite stalled at a pre-existing FastAPI TestClient test in the sandbox; it passed when rerun outside the sandbox.

## Remaining limitations

- A nonzero `systemctl` exit can be reported as service-manager unavailable even when the underlying cause is a permission or other remote command error. The result is SKIP rather than a claim that the application has failed; the classification needs further verification against a real Linux host.
- Phase 5.9 has not yet been validated against a real Linux host. The automated results above do not establish real-host behavior.
