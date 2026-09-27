from __future__ import annotations

from dataclasses import dataclass
import math
import re

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.infrastructure.process_runner import ProcessResult, ProcessRunner
from app.services.remote_probe import RemoteProbeService


_LOAD_COMMAND = "set -eu\ncat -- /proc/loadavg\ngetconf _NPROCESSORS_ONLN"
_PROCESS_COMMAND = (
    "set -eu\n"
    "output=$(LC_ALL=C ps -eo comm=,pcpu=,pmem= --sort=-pcpu)\n"
    "printf '%s\\n' \"$output\" | head -n 5"
)
_FAILED_SERVICES_COMMAND = (
    "set -eu\n"
    "output=$(systemctl --failed --type=service --no-legend --plain --no-pager)\n"
    "printf '%s\\n' \"$output\" | "
    "awk '{ if ($1 == \"●\") print $2; else print $1 }'"
)
_SAFE_PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9_.+@:/()\[\] \-]{1,64}$")
_SAFE_SERVICE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,247}\.service$")
_PROCESS_LIMIT = 5
_FAILED_SERVICE_LIMIT = 20


@dataclass(frozen=True)
class RemoteLoadReport:
    check: CheckResult
    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None
    cpu_count: int | None = None
    load_1m_per_cpu: float | None = None


@dataclass(frozen=True)
class RemoteProcessEntry:
    name: str
    cpu_percent: float
    memory_percent: float

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "cpu_percent": self.cpu_percent,
            "memory_percent": self.memory_percent,
        }


@dataclass(frozen=True)
class RemoteProcessSnapshot:
    check: CheckResult
    processes: tuple[RemoteProcessEntry, ...] = ()


@dataclass(frozen=True)
class RemoteServiceReport:
    check: CheckResult
    failed_services: tuple[str, ...] = ()
    truncated: bool = False


class RemoteRuntimeService:
    def __init__(
        self,
        runner: ProcessRunner,
        remote_probe: RemoteProbeService,
        *,
        timeout: float = 15.0,
        load_per_cpu_threshold: float = 2.0,
    ) -> None:
        self.timeout = _bounded_number(timeout, "remote runtime timeout", minimum=0, maximum=60)
        self.load_per_cpu_threshold = _bounded_number(
            load_per_cpu_threshold,
            "load per CPU threshold",
            minimum=0,
            maximum=1000,
        )
        self.runner = runner
        self.remote_probe = remote_probe

    def check_load(self, profile: Profile) -> RemoteLoadReport:
        result = self._run(profile, _LOAD_COMMAND)
        unavailable = self._transport_failure(result, "Remote load")
        if unavailable is not None:
            return RemoteLoadReport(unavailable)
        try:
            load_1m, load_5m, load_15m, cpu_count = _parse_load(result.stdout)
        except ValueError:
            return RemoteLoadReport(
                CheckResult(
                    "Remote load",
                    CheckStatus.FAIL,
                    "remote load information was invalid",
                    "REMOTE_LOAD_INFO_INVALID",
                )
            )
        load_1m_per_cpu = round(load_1m / cpu_count, 3)
        if load_1m_per_cpu >= self.load_per_cpu_threshold:
            check = CheckResult(
                "Remote load",
                CheckStatus.FAIL,
                "remote load reached the configured per-CPU threshold",
                "REMOTE_LOAD_PRESSURE",
            )
        else:
            check = CheckResult(
                "Remote load",
                CheckStatus.PASS,
                "remote load is within the configured per-CPU threshold",
            )
        return RemoteLoadReport(
            check,
            load_1m,
            load_5m,
            load_15m,
            cpu_count,
            load_1m_per_cpu,
        )

    def get_top_processes(self, profile: Profile) -> RemoteProcessSnapshot:
        result = self._run(profile, _PROCESS_COMMAND)
        unavailable = self._transport_failure(
            result,
            "Remote process snapshot",
            error_code="REMOTE_PROCESS_SNAPSHOT_UNAVAILABLE",
        )
        if unavailable is not None:
            return RemoteProcessSnapshot(unavailable)
        try:
            processes = _parse_processes(result.stdout)
        except ValueError:
            return RemoteProcessSnapshot(
                CheckResult(
                    "Remote process snapshot",
                    CheckStatus.FAIL,
                    "remote process snapshot was invalid",
                    "REMOTE_PROCESS_SNAPSHOT_INVALID",
                )
            )
        return RemoteProcessSnapshot(
            CheckResult(
                "Remote process snapshot",
                CheckStatus.PASS,
                "remote process snapshot was read successfully",
            ),
            processes,
        )

    def check_failed_services(self, profile: Profile) -> RemoteServiceReport:
        result = self._run(profile, _FAILED_SERVICES_COMMAND)
        if not result.ok:
            if result.timed_out or result.exit_code in (None, 255):
                check = CheckResult(
                    "Remote failed services",
                    CheckStatus.FAIL,
                    "remote service check was unavailable",
                    "REMOTE_SYSTEM_UNAVAILABLE",
                )
            else:
                check = CheckResult(
                    "Remote failed services",
                    CheckStatus.SKIP,
                    "remote service manager is unavailable",
                    "REMOTE_SERVICE_MANAGER_UNAVAILABLE",
                )
            return RemoteServiceReport(check)
        try:
            services, truncated = _parse_failed_services(result.stdout)
        except ValueError:
            return RemoteServiceReport(
                CheckResult(
                    "Remote failed services",
                    CheckStatus.FAIL,
                    "remote failed service information was invalid",
                    "REMOTE_SERVICE_INFO_INVALID",
                )
            )
        if services:
            check = CheckResult(
                "Remote failed services",
                CheckStatus.FAIL,
                "one or more remote services are in a failed state",
                "REMOTE_FAILED_SERVICES",
            )
        else:
            check = CheckResult(
                "Remote failed services",
                CheckStatus.PASS,
                "no failed remote services were reported",
            )
        return RemoteServiceReport(check, services, truncated)

    def _run(self, profile: Profile, command: str) -> ProcessResult:
        return self.runner.run(
            self.remote_probe.ssh_prefix(profile) + [command],
            timeout=self.timeout,
        )

    @staticmethod
    def _transport_failure(
        result: ProcessResult,
        name: str,
        *,
        error_code: str = "REMOTE_SYSTEM_UNAVAILABLE",
    ) -> CheckResult | None:
        if result.ok:
            return None
        detail = (
            "remote runtime check timed out"
            if result.timed_out
            else "remote runtime check was unavailable"
        )
        return CheckResult(name, CheckStatus.FAIL, detail, error_code)


def _parse_load(output: str) -> tuple[float, float, float, int]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 2:
        raise ValueError("invalid load information")
    load_fields = lines[0].split()
    if len(load_fields) < 3:
        raise ValueError("incomplete load information")
    load_values = tuple(_finite_nonnegative(value) for value in load_fields[:3])
    if not lines[1].isdigit():
        raise ValueError("invalid CPU count")
    cpu_count = int(lines[1])
    if cpu_count <= 0:
        raise ValueError("invalid CPU count")
    return load_values[0], load_values[1], load_values[2], cpu_count


def _parse_processes(output: str) -> tuple[RemoteProcessEntry, ...]:
    entries: list[RemoteProcessEntry] = []
    for line in (item.strip() for item in output.splitlines() if item.strip()):
        fields = line.rsplit(maxsplit=2)
        if len(fields) != 3 or not _SAFE_PROCESS_NAME_RE.fullmatch(fields[0]):
            raise ValueError("invalid process snapshot")
        cpu_percent = _finite_nonnegative(fields[1])
        memory_percent = _finite_nonnegative(fields[2])
        if memory_percent > 100:
            raise ValueError("invalid process memory percentage")
        entries.append(RemoteProcessEntry(fields[0], cpu_percent, memory_percent))
        if len(entries) == _PROCESS_LIMIT:
            break
    return tuple(entries)


def _parse_failed_services(output: str) -> tuple[tuple[str, ...], bool]:
    units = tuple(line.strip() for line in output.splitlines() if line.strip())
    if len(units) != len(set(units)):
        raise ValueError("duplicate failed service unit")
    if any(not _SAFE_SERVICE_UNIT_RE.fullmatch(unit) for unit in units):
        raise ValueError("invalid failed service unit")
    return units[:_FAILED_SERVICE_LIMIT], len(units) > _FAILED_SERVICE_LIMIT


def _finite_nonnegative(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError("invalid numeric value") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError("invalid numeric value")
    return parsed


def _bounded_number(
    value: object,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum < normalized <= maximum:
        raise ValueError(f"{label} is outside its safe range")
    return normalized
