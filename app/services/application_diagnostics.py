from __future__ import annotations

from dataclasses import dataclass
import re
import shlex

from app.domain.application_target import ApplicationTarget
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.infrastructure.process_runner import ProcessResult, ProcessRunner
from app.services.remote_probe import RemoteProbeService


_SERVICE_FIELDS = frozenset({"LoadState", "ActiveState", "SubState"})
_SAFE_SERVICE_STATE_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HTTP_STATUS_RE = re.compile(r"^[0-9]{3}$")


@dataclass(frozen=True)
class ApplicationServiceReport:
    check: CheckResult
    target_name: str
    active_state: str | None = None
    sub_state: str | None = None


@dataclass(frozen=True)
class ApplicationHealthReport:
    check: CheckResult
    target_name: str
    http_status: int | None = None


class ApplicationDiagnosticsService:
    def __init__(
        self,
        runner: ProcessRunner,
        remote_probe: RemoteProbeService,
        *,
        timeout: float = 15.0,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("application diagnostics timeout must be numeric")
        if not 0 < float(timeout) <= 60:
            raise ValueError("application diagnostics timeout is outside its safe range")
        self.runner = runner
        self.remote_probe = remote_probe
        self.timeout = float(timeout)

    def check_service(
        self,
        profile: Profile,
        target: ApplicationTarget,
    ) -> ApplicationServiceReport:
        profile.validate()
        target.validate()
        if target.profile_name != profile.name:
            raise ValueError("application target does not belong to profile")
        if target.service_unit is None:
            return ApplicationServiceReport(
                CheckResult(
                    "Application service",
                    CheckStatus.SKIP,
                    "application service is not configured for this target",
                    "APPLICATION_SERVICE_NOT_CONFIGURED",
                ),
                target.name,
            )
        command = " ".join(
            [
                "LC_ALL=C",
                "systemctl",
                "show",
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=SubState",
                "--",
                shlex.quote(target.service_unit),
            ]
        )
        result = self._run(profile, command)
        if not result.ok:
            if result.timed_out or result.exit_code in (None, 255):
                check = CheckResult(
                    "Application service",
                    CheckStatus.FAIL,
                    "application service status could not be reached",
                    "APPLICATION_SERVICE_STATUS_UNAVAILABLE",
                )
            else:
                check = CheckResult(
                    "Application service",
                    CheckStatus.SKIP,
                    "remote service manager is unavailable",
                    "APPLICATION_SERVICE_MANAGER_UNAVAILABLE",
                )
            return ApplicationServiceReport(check, target.name)
        try:
            states = _parse_service_status(result.stdout)
        except ValueError:
            return ApplicationServiceReport(
                CheckResult(
                    "Application service",
                    CheckStatus.FAIL,
                    "application service status was invalid",
                    "APPLICATION_SERVICE_STATUS_INVALID",
                ),
                target.name,
            )
        active_state = states["ActiveState"]
        sub_state = states["SubState"]
        if states["LoadState"] == "not-found":
            check = CheckResult(
                "Application service",
                CheckStatus.FAIL,
                "configured application service was not found",
                "APPLICATION_SERVICE_NOT_FOUND",
            )
        elif states["LoadState"] == "loaded" and active_state == "active":
            check = CheckResult(
                "Application service",
                CheckStatus.PASS,
                "application service is active",
            )
        else:
            check = CheckResult(
                "Application service",
                CheckStatus.FAIL,
                "application service is not active",
                "APPLICATION_SERVICE_INACTIVE",
            )
        return ApplicationServiceReport(check, target.name, active_state, sub_state)

    def check_health(
        self,
        profile: Profile,
        target: ApplicationTarget,
    ) -> ApplicationHealthReport:
        profile.validate()
        target.validate()
        if target.profile_name != profile.name:
            raise ValueError("application target does not belong to profile")
        if target.health_url is None:
            return ApplicationHealthReport(
                CheckResult(
                    "Application health",
                    CheckStatus.SKIP,
                    "application health endpoint is not configured for this target",
                    "APPLICATION_HEALTH_NOT_CONFIGURED",
                ),
                target.name,
            )
        command = " ".join(
            [
                "curl",
                "--disable",
                "--globoff",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--write-out",
                shlex.quote("%{http_code}"),
                "--max-time",
                str(max(1, int(self.timeout))),
                "--noproxy",
                shlex.quote("*"),
                "--",
                shlex.quote(target.health_url),
            ]
        )
        result = self._run(profile, command, timeout=self.timeout + 5)
        if not result.ok:
            timed_out = result.timed_out or result.exit_code == 28
            error_code = (
                "APPLICATION_HEALTH_TIMEOUT"
                if timed_out
                else "APPLICATION_HEALTH_UNREACHABLE"
            )
            detail = (
                "application health check timed out"
                if timed_out
                else "application health endpoint was unreachable"
            )
            return ApplicationHealthReport(
                CheckResult("Application health", CheckStatus.FAIL, detail, error_code),
                target.name,
            )
        raw_status = result.stdout.strip()
        if not _HTTP_STATUS_RE.fullmatch(raw_status):
            return ApplicationHealthReport(
                CheckResult(
                    "Application health",
                    CheckStatus.FAIL,
                    "application health endpoint returned an invalid HTTP status",
                    "APPLICATION_HEALTH_INVALID_RESPONSE",
                ),
                target.name,
            )
        status = int(raw_status)
        if not 100 <= status <= 599:
            return ApplicationHealthReport(
                CheckResult(
                    "Application health",
                    CheckStatus.FAIL,
                    "application health endpoint returned an invalid HTTP status",
                    "APPLICATION_HEALTH_INVALID_RESPONSE",
                ),
                target.name,
            )
        if 200 <= status < 300:
            check = CheckResult(
                "Application health",
                CheckStatus.PASS,
                f"application health endpoint returned HTTP {status}",
                http_status=status,
            )
        else:
            check = CheckResult(
                "Application health",
                CheckStatus.FAIL,
                f"application health endpoint returned HTTP {status}",
                "APPLICATION_HEALTH_UNHEALTHY",
                status,
            )
        return ApplicationHealthReport(check, target.name, status)

    def _run(
        self,
        profile: Profile,
        command: str,
        *,
        timeout: float | None = None,
    ) -> ProcessResult:
        return self.runner.run(
            self.remote_probe.ssh_prefix(profile) + [command],
            timeout=self.timeout if timeout is None else timeout,
        )


def _parse_service_status(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if (
            not separator
            or key not in _SERVICE_FIELDS
            or key in fields
            or not _SAFE_SERVICE_STATE_RE.fullmatch(value)
        ):
            raise ValueError("invalid application service status")
        fields[key] = value
    if set(fields) != _SERVICE_FIELDS:
        raise ValueError("incomplete application service status")
    return fields
