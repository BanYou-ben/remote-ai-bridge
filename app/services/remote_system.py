from __future__ import annotations

from dataclasses import dataclass
import re
import shlex

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.infrastructure.process_runner import ProcessResult, ProcessRunner
from app.services.remote_probe import RemoteProbeService


_SYSTEM_INFO_COMMAND = """set -eu
printf 'os_name='
sed -n 's/^NAME=//p' /etc/os-release | head -n 1
printf 'os_version='
sed -n 's/^VERSION_ID=//p' /etc/os-release | head -n 1
printf 'kernel='
uname -r
printf 'architecture='
uname -m"""
_MEMORY_COMMAND = "awk '/^(MemTotal|MemAvailable):/ { print }' /proc/meminfo"
_DISK_COMMAND = "df -Pk -- /"
_MEMORY_LINE_RE = re.compile(r"^(MemTotal|MemAvailable):\s+([0-9]+)\s+kB$")
_SAFE_SYSTEM_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+\-]+$")


@dataclass(frozen=True)
class RemoteSystemInfo:
    check: CheckResult
    os_name: str | None = None
    os_version: str | None = None
    kernel: str | None = None
    architecture: str | None = None


@dataclass(frozen=True)
class RemoteMemoryReport:
    check: CheckResult
    total_bytes: int | None = None
    available_bytes: int | None = None
    available_percent: float | None = None


@dataclass(frozen=True)
class RemoteDiskReport:
    check: CheckResult
    total_bytes: int | None = None
    used_bytes: int | None = None
    available_bytes: int | None = None
    used_percent: float | None = None


class RemoteSystemService:
    def __init__(
        self,
        runner: ProcessRunner,
        remote_probe: RemoteProbeService,
        *,
        timeout: float = 15.0,
        memory_available_threshold: float = 5.0,
        disk_used_threshold: float = 95.0,
    ) -> None:
        self.timeout = _bounded_number(timeout, "remote system timeout", minimum=0, maximum=60)
        self.memory_available_threshold = _bounded_number(
            memory_available_threshold,
            "memory available threshold",
            minimum=0,
            maximum=100,
            include_minimum=True,
        )
        self.disk_used_threshold = _bounded_number(
            disk_used_threshold,
            "disk used threshold",
            minimum=0,
            maximum=100,
            include_minimum=True,
        )
        self.runner = runner
        self.remote_probe = remote_probe

    def get_system_info(self, profile: Profile) -> RemoteSystemInfo:
        result = self._run(profile, _SYSTEM_INFO_COMMAND)
        unavailable = self._unavailable(result, "Remote system information")
        if unavailable is not None:
            return RemoteSystemInfo(unavailable)
        try:
            fields = _parse_system_info(result.stdout)
        except ValueError:
            return RemoteSystemInfo(
                CheckResult(
                    "Remote system information",
                    CheckStatus.FAIL,
                    "remote system information was invalid",
                    "REMOTE_SYSTEM_INFO_INVALID",
                )
            )
        return RemoteSystemInfo(
            CheckResult(
                "Remote system information",
                CheckStatus.PASS,
                "remote system information was read successfully",
            ),
            fields["os_name"],
            fields["os_version"],
            fields["kernel"],
            fields["architecture"],
        )

    def check_memory(self, profile: Profile) -> RemoteMemoryReport:
        result = self._run(profile, _MEMORY_COMMAND)
        unavailable = self._unavailable(result, "Remote memory")
        if unavailable is not None:
            return RemoteMemoryReport(unavailable)
        try:
            total_bytes, available_bytes = _parse_memory(result.stdout)
        except ValueError:
            return RemoteMemoryReport(
                CheckResult(
                    "Remote memory",
                    CheckStatus.FAIL,
                    "remote memory information was invalid",
                    "REMOTE_MEMORY_INFO_INVALID",
                )
            )
        available_percent = round(available_bytes * 100.0 / total_bytes, 2)
        if available_percent < self.memory_available_threshold:
            check = CheckResult(
                "Remote memory",
                CheckStatus.FAIL,
                "remote available memory is below the configured safety threshold",
                "REMOTE_MEMORY_PRESSURE",
            )
        else:
            check = CheckResult(
                "Remote memory",
                CheckStatus.PASS,
                "remote available memory is within the configured safety threshold",
            )
        return RemoteMemoryReport(check, total_bytes, available_bytes, available_percent)

    def check_disk(self, profile: Profile) -> RemoteDiskReport:
        result = self._run(profile, _DISK_COMMAND)
        unavailable = self._unavailable(result, "Remote root filesystem")
        if unavailable is not None:
            return RemoteDiskReport(unavailable)
        try:
            total_bytes, used_bytes, available_bytes, used_percent = _parse_disk(
                result.stdout
            )
        except ValueError:
            return RemoteDiskReport(
                CheckResult(
                    "Remote root filesystem",
                    CheckStatus.FAIL,
                    "remote root filesystem information was invalid",
                    "REMOTE_DISK_INFO_INVALID",
                )
            )
        if used_percent >= self.disk_used_threshold:
            check = CheckResult(
                "Remote root filesystem",
                CheckStatus.FAIL,
                "remote root filesystem usage reached the configured safety threshold",
                "REMOTE_DISK_PRESSURE",
            )
        else:
            check = CheckResult(
                "Remote root filesystem",
                CheckStatus.PASS,
                "remote root filesystem usage is within the configured safety threshold",
            )
        return RemoteDiskReport(
            check,
            total_bytes,
            used_bytes,
            available_bytes,
            used_percent,
        )

    def _run(self, profile: Profile, command: str) -> ProcessResult:
        return self.runner.run(
            self.remote_probe.ssh_prefix(profile) + [command],
            timeout=self.timeout,
        )

    @staticmethod
    def _unavailable(result: ProcessResult, name: str) -> CheckResult | None:
        if result.ok:
            return None
        detail = (
            "remote system check timed out"
            if result.timed_out
            else "remote system check was unavailable"
        )
        return CheckResult(
            name,
            CheckStatus.FAIL,
            detail,
            "REMOTE_SYSTEM_UNAVAILABLE",
        )


def _parse_system_info(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, raw_value = line.partition("=")
        if not separator or key in fields:
            raise ValueError("invalid system information")
        fields[key] = _clean_system_value(raw_value)
    required = {"os_name", "os_version", "kernel", "architecture"}
    if set(fields) != required:
        raise ValueError("incomplete system information")
    for key in ("kernel", "architecture"):
        if not _SAFE_SYSTEM_TOKEN_RE.fullmatch(fields[key]):
            raise ValueError("invalid system token")
    return fields


def _clean_system_value(value: str) -> str:
    try:
        parts = shlex.split(value, posix=True)
    except ValueError as exc:
        raise ValueError("invalid system value") from exc
    if len(parts) != 1 or not parts[0] or len(parts[0]) > 200:
        raise ValueError("invalid system value")
    if any(ord(character) < 32 or ord(character) == 127 for character in parts[0]):
        raise ValueError("invalid system value")
    return parts[0]


def _parse_memory(output: str) -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in output.splitlines():
        match = _MEMORY_LINE_RE.fullmatch(line.strip())
        if match is None or match.group(1) in values:
            raise ValueError("invalid memory information")
        values[match.group(1)] = int(match.group(2)) * 1024
    if set(values) != {"MemTotal", "MemAvailable"}:
        raise ValueError("incomplete memory information")
    total = values["MemTotal"]
    available = values["MemAvailable"]
    if total <= 0 or available < 0 or available > total:
        raise ValueError("invalid memory values")
    return total, available


def _parse_disk(output: str) -> tuple[int, int, int, float]:
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) != 2:
        raise ValueError("invalid disk information")
    columns = lines[1].split()
    if len(columns) != 6 or columns[-1] != "/":
        raise ValueError("invalid root filesystem information")
    try:
        total, used, available = (int(columns[index]) * 1024 for index in (1, 2, 3))
    except ValueError as exc:
        raise ValueError("invalid disk values") from exc
    capacity = columns[4]
    if not capacity.endswith("%") or not capacity[:-1].isdigit():
        raise ValueError("invalid disk capacity")
    used_percent = float(capacity[:-1])
    if (
        total <= 0
        or used < 0
        or available < 0
        or used > total
        or available > total
        or used + available > total
        or not 0 <= used_percent <= 100
    ):
        raise ValueError("invalid disk values")
    return total, used, available, used_percent


def _bounded_number(
    value: object,
    label: str,
    *,
    minimum: float,
    maximum: float,
    include_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    valid_minimum = value >= minimum if include_minimum else value > minimum
    if not valid_minimum or value > maximum:
        raise ValueError(f"{label} is outside its safe range")
    return float(value)
