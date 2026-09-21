from __future__ import annotations

import shlex
import uuid
from pathlib import Path

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile, ProfileValidationError, RemoteTunnelIdentity, RuntimeState
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.profile_store import default_state_root
from app.infrastructure.ssh_key_store import managed_ssh_options


class RemoteProbeService:
    def __init__(
        self,
        runner: ProcessRunner,
        ssh_executable: str,
        timeout: float = 15.0,
        state_root: Path | None = None,
    ) -> None:
        self.runner = runner
        self.ssh_executable = ssh_executable
        self.timeout = timeout
        self.state_root = state_root or default_state_root()

    def ssh_prefix(self, profile: Profile) -> list[str]:
        prefix = [
            self.ssh_executable,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={max(1, int(self.timeout))}",
        ]
        prefix.extend(managed_ssh_options(profile, self.state_root))
        prefix.append(profile.ssh_target)
        return prefix

    def check_listener(self, profile: Profile, port: int | None = None) -> CheckResult:
        checked_port = port if port is not None else profile.remote_port
        remote_command = f"ss -ltnH 'sport = :{checked_port}'"
        result = self.runner.run(self.ssh_prefix(profile) + [remote_command], timeout=self.timeout)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "remote listener check failed"
            return CheckResult("Remote listener", CheckStatus.FAIL, detail[:300], "REMOTE_CHECK_FAILED")
        if result.stdout.strip():
            expected = f"{profile.remote_bind_host}:{checked_port}"
            if expected not in result.stdout:
                return CheckResult(
                    "Remote listener",
                    CheckStatus.FAIL,
                    f"port {checked_port} is listening but not on the required {expected} binding",
                    "UNSAFE_REMOTE_BINDING",
                )
            return CheckResult(
                "Remote listener",
                CheckStatus.PASS,
                f"{profile.remote_bind_host}:{checked_port} is listening",
            )
        return CheckResult(
            "Remote listener",
            CheckStatus.FAIL,
            f"no listener on {profile.remote_bind_host}:{checked_port}",
            "LISTENER_ABSENT",
        )

    def check_endpoint(self, profile: Profile) -> CheckResult:
        proxy = f"http://{profile.remote_bind_host}:{profile.remote_port}"
        remote_command = " ".join(
            [
                "curl",
                "--disable",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--write-out",
                shlex.quote("%{http_code}"),
                "--max-time",
                str(max(1, int(self.timeout))),
                "--proxy",
                shlex.quote(proxy),
                "--noproxy",
                shlex.quote(""),
                shlex.quote(profile.endpoint_probe_url),
            ]
        )
        result = self.runner.run(self.ssh_prefix(profile) + [remote_command], timeout=self.timeout + 5)
        if not result.ok:
            detail = result.stderr.strip() or result.stdout.strip() or "remote endpoint probe failed"
            error = "REMOTE_ENDPOINT_TIMEOUT" if result.timed_out else "REMOTE_ENDPOINT_UNREACHABLE"
            return CheckResult("Remote endpoint probe", CheckStatus.FAIL, detail[:300], error)
        raw_status = result.stdout.strip()
        try:
            status = int(raw_status[-3:])
        except ValueError:
            return CheckResult("Remote endpoint probe", CheckStatus.FAIL, "curl returned no HTTP status", "INVALID_PROBE_OUTPUT")
        if status in (401, 403) or 200 <= status < 300:
            return CheckResult(
                "Remote endpoint probe",
                CheckStatus.PASS,
                f"endpoint returned HTTP {status}; bridge networking is healthy",
                http_status=status,
            )
        return CheckResult(
            "Remote endpoint probe",
            CheckStatus.FAIL,
            f"unexpected endpoint response HTTP {status}",
            "ENDPOINT_UNEXPECTED_STATUS",
            status,
        )

    def read_tunnel_identity(
        self, profile: Profile, tunnel_id: str
    ) -> tuple[RemoteTunnelIdentity | None, CheckResult]:
        _validate_tunnel_id(tunnel_id)
        marker = f"$HOME/.remote-ai-bridge/runtime/{tunnel_id}.state"
        remote_command = f'test -f "{marker}" && cat -- "{marker}"'
        result = self.runner.run(self.ssh_prefix(profile) + [remote_command], timeout=self.timeout)
        if not result.ok:
            detail = result.stderr.strip() or "remote tunnel identity is not available"
            return None, CheckResult("Remote tunnel identity", CheckStatus.FAIL, detail[:300], "REMOTE_IDENTITY_UNAVAILABLE")
        fields: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key not in fields:
                fields[key] = value
        try:
            identity = RemoteTunnelIdentity(
                tunnel_id=fields["tunnel_id"],
                remote_port=int(fields["remote_port"]),
                remote_user=fields["remote_user"],
                sshd_pid=int(fields["sshd_pid"]),
                sshd_start_ticks=int(fields["sshd_start_ticks"]),
                boot_id=fields["boot_id"],
                ssh_connection=fields["ssh_connection"],
            )
            identity.validate()
        except (KeyError, ValueError, ProfileValidationError) as exc:
            return None, CheckResult(
                "Remote tunnel identity",
                CheckStatus.FAIL,
                f"invalid remote identity marker: {exc}",
                "REMOTE_IDENTITY_INVALID",
            )
        if identity.tunnel_id != tunnel_id or identity.remote_port != profile.remote_port:
            return None, CheckResult(
                "Remote tunnel identity",
                CheckStatus.FAIL,
                "remote identity does not match the requested tunnel and port",
                "REMOTE_IDENTITY_MISMATCH",
            )
        return identity, CheckResult(
            "Remote tunnel identity",
            CheckStatus.PASS,
            f"verified marker for remote user {identity.remote_user} and sshd PID {identity.sshd_pid}",
        )

    def verify_stale_session_ownership(self, profile: Profile, state: RuntimeState) -> CheckResult:
        if not state.has_remote_identity:
            return CheckResult(
                "Remote stale session ownership",
                CheckStatus.FAIL,
                "runtime has no verified remote session identity",
                "REMOTE_IDENTITY_UNAVAILABLE",
            )
        return self._run_identity_action(profile, state.remote_identity(), terminate=False)

    def terminate_verified_stale_session(self, profile: Profile, state: RuntimeState) -> CheckResult:
        if not state.has_remote_identity:
            return CheckResult(
                "Remote stale session cleanup",
                CheckStatus.FAIL,
                "runtime has no verified remote session identity",
                "REMOTE_IDENTITY_UNAVAILABLE",
            )
        return self._run_identity_action(profile, state.remote_identity(), terminate=True)

    def _run_identity_action(
        self, profile: Profile, identity: RemoteTunnelIdentity, terminate: bool
    ) -> CheckResult:
        identity.validate()
        remote_command = _build_identity_validation_command(identity, terminate)
        result = self.runner.run(self.ssh_prefix(profile) + [remote_command], timeout=self.timeout)
        name = "Remote stale session cleanup" if terminate else "Remote stale session ownership"
        if result.ok and result.stdout.strip().endswith("CLEANED" if terminate else "VERIFIED"):
            detail = (
                f"terminated verified stale sshd session PID {identity.sshd_pid}"
                if terminate
                else f"listener and sshd session PID {identity.sshd_pid} match saved tunnel ownership"
            )
            return CheckResult(name, CheckStatus.PASS, detail)
        detail = result.stderr.strip() or result.stdout.strip() or "remote identity could not be verified"
        error = "REMOTE_STALE_CLEANUP_FAILED" if terminate else "REMOTE_IDENTITY_MISMATCH"
        return CheckResult(name, CheckStatus.FAIL, detail[:300], error)

    def find_free_port(self, profile: Profile, attempts: int = 20) -> int | None:
        for candidate in range(profile.remote_port + 1, min(65536, profile.remote_port + attempts + 1)):
            result = self.check_listener(profile, candidate)
            if result.error_code == "LISTENER_ABSENT":
                return candidate
            if result.error_code == "REMOTE_CHECK_FAILED":
                return None
        return None


def build_remote_identity_command(tunnel_id: str, remote_port: int) -> str:
    _validate_tunnel_id(tunnel_id)
    if not 1 <= remote_port <= 65535:
        raise ValueError("invalid remote port")
    marker = f'$HOME/.remote-ai-bridge/runtime/{tunnel_id}.state'
    return "\n".join(
        [
            "set -eu",
            "umask 077",
            'base="$HOME/.remote-ai-bridge"',
            'runtime_dir="$base/runtime"',
            'mkdir -p -- "$runtime_dir"',
            'chmod 700 -- "$base" "$runtime_dir"',
            f'marker="{marker}"',
            "sshd_pid=$PPID",
            r'sshd_start_ticks=$(awk "{print \$22}" "/proc/$sshd_pid/stat")',
            'boot_id=$(cat /proc/sys/kernel/random/boot_id)',
            'remote_user=$(id -un)',
            'ssh_connection=${SSH_CONNECTION:-}',
            '[ -n "$ssh_connection" ] || { echo "missing SSH_CONNECTION" >&2; exit 71; }',
            "attempt=0",
            "while [ \"$attempt\" -lt 50 ]; do",
            f"  listener_line=$(ss -ltnH 'sport = :{remote_port}' 2>/dev/null || true)",
            f"  printf '%s\\n' \"$listener_line\" | grep -F -- '127.0.0.1:{remote_port}' >/dev/null && break",
            "  attempt=$((attempt + 1))",
            "  sleep 0.1",
            "done",
            f"printf '%s\\n' \"$listener_line\" | grep -F -- '127.0.0.1:{remote_port}' >/dev/null || "
            '{ echo "reverse listener was not established on the required loopback port" >&2; exit 72; }',
            'temporary="$marker.$$"',
            "{",
            "  printf '%s\\n' 'schema_version=1'",
            f"  printf 'tunnel_id=%s\\n' '{tunnel_id}'",
            f"  printf 'remote_port=%s\\n' '{remote_port}'",
            "  printf 'remote_user=%s\\n' \"$remote_user\"",
            "  printf 'sshd_pid=%s\\n' \"$sshd_pid\"",
            "  printf 'sshd_start_ticks=%s\\n' \"$sshd_start_ticks\"",
            "  printf 'boot_id=%s\\n' \"$boot_id\"",
            "  printf 'ssh_connection=%s\\n' \"$ssh_connection\"",
            '} > "$temporary"',
            'chmod 600 -- "$temporary"',
            'mv -f -- "$temporary" "$marker"',
            'cleanup_marker() { rm -f -- "$marker" "$temporary"; }',
            "trap 'cleanup_marker; exit 0' HUP INT TERM",
            "trap cleanup_marker EXIT",
            "while :; do sleep 30; done",
        ]
    )


def _build_identity_validation_command(identity: RemoteTunnelIdentity, terminate: bool) -> str:
    marker = f'$HOME/.remote-ai-bridge/runtime/{identity.tunnel_id}.state'
    expected = {
        "tunnel_id": identity.tunnel_id,
        "remote_port": str(identity.remote_port),
        "remote_user": identity.remote_user,
        "sshd_pid": str(identity.sshd_pid),
        "sshd_start_ticks": str(identity.sshd_start_ticks),
        "boot_id": identity.boot_id,
        "ssh_connection": identity.ssh_connection,
    }
    checks = [
        "set -eu",
        f'marker="{marker}"',
        '[ -f "$marker" ] || { echo "identity marker unavailable" >&2; exit 81; }',
        '[ "$(stat -c %u -- "$marker")" = "$(id -u)" ] || { echo "marker owner mismatch" >&2; exit 82; }',
        '[ "$(stat -c %a -- "$marker")" = "600" ] || { echo "marker mode mismatch" >&2; exit 83; }',
        "field() { sed -n \"s/^$1=//p\" \"$marker\" | head -n 1; }",
    ]
    for key, value in expected.items():
        checks.append(
            f'[ "$(field {shlex.quote(key)})" = {shlex.quote(value)} ] || '
            f'{{ echo "marker {key} mismatch" >&2; exit 84; }}'
        )
    checks.extend(
        [
            f'pid={identity.sshd_pid}',
            f'port={identity.remote_port}',
            '[ "$(id -un)" = "$(field remote_user)" ] || { echo "remote user mismatch" >&2; exit 85; }',
            '[ -r "/proc/$pid/stat" ] || { echo "sshd process unavailable" >&2; exit 86; }',
            '[ "$(stat -c %u -- "/proc/$pid")" = "$(id -u)" ] || { echo "sshd process owner mismatch" >&2; exit 87; }',
            r'[ "$(awk "{print \$22}" "/proc/$pid/stat")" = "$(field sshd_start_ticks)" ] || { echo "sshd start time mismatch" >&2; exit 88; }',
            '[ "$(cat /proc/sys/kernel/random/boot_id)" = "$(field boot_id)" ] || { echo "remote boot ID mismatch" >&2; exit 89; }',
            'comm=$(cat "/proc/$pid/comm")',
            'case "$comm" in sshd|sshd-session) ;; *) echo "process is not sshd" >&2; exit 90;; esac',
            'listener_line=$(ss -ltnH "sport = :$port" 2>/dev/null || true)',
            'printf "%s\\n" "$listener_line" | grep -F -- "127.0.0.1:$port" >/dev/null || { echo "loopback listener mismatch" >&2; exit 91; }',
        ]
    )
    if not terminate:
        checks.append("printf 'VERIFIED\\n'")
        return "\n".join(checks)
    checks.extend(
        [
            # Re-reading the marker immediately before the signal closes the verification/termination gap.
            '[ "$(field tunnel_id)" = ' + shlex.quote(identity.tunnel_id) + ' ] || exit 92',
            r'[ "$(awk "{print \$22}" "/proc/$pid/stat")" = "$(field sshd_start_ticks)" ] || exit 93',
            'kill -TERM -- "$pid"',
            "attempt=0",
            "while [ \"$attempt\" -lt 50 ]; do",
            '  listener_line=$(ss -ltnH "sport = :$port" 2>/dev/null || true)',
            '  if [ -z "$listener_line" ]; then rm -f -- "$marker"; printf "CLEANED\\n"; exit 0; fi',
            "  attempt=$((attempt + 1))",
            "  sleep 0.1",
            "done",
            'echo "verified sshd session did not release listener" >&2',
            "exit 94",
        ]
    )
    return "\n".join(checks)


def _validate_tunnel_id(tunnel_id: str) -> None:
    try:
        parsed = uuid.UUID(tunnel_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("tunnel ID must be a UUID") from exc
    if str(parsed) != tunnel_id.lower():
        raise ValueError("tunnel ID must use canonical UUID form")
