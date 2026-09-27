from __future__ import annotations

import json

import pytest

from app.domain.health import CheckStatus
from app.domain.profile import Profile
from app.infrastructure.process_runner import ProcessResult
from app.services.remote_system import RemoteSystemService


def _profile(*, legacy: bool = False) -> Profile:
    if legacy:
        return Profile(schema_version=1, name="legacy", ssh_target="trusted-alias")
    return Profile(
        schema_version=2,
        name="server",
        ssh_target="alice@ssh.example.test",
        profile_type="managed",
        host="ssh.example.test",
        username="alice",
        ssh_port=2222,
        key_id="1" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:" + "A" * 43,
    )


class FakeRunner:
    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, argv, timeout: float) -> ProcessResult:
        self.calls.append((tuple(argv), timeout))
        return self.result


class FakeRemoteProbe:
    def __init__(self) -> None:
        self.profiles: list[Profile] = []

    def ssh_prefix(self, profile: Profile) -> list[str]:
        profile.validate()
        self.profiles.append(profile)
        return [
            "ssh.exe",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            profile.ssh_target,
        ]


def _result(
    stdout: str = "",
    *,
    exit_code: int | None = 0,
    stderr: str = "",
    error_code: str | None = None,
    timed_out: bool = False,
) -> ProcessResult:
    return ProcessResult(("ssh.exe",), exit_code, stdout, stderr, error_code, timed_out)


def _service(result: ProcessResult, **kwargs):
    runner = FakeRunner(result)
    probe = FakeRemoteProbe()
    return RemoteSystemService(runner, probe, **kwargs), runner, probe


def test_system_info_parses_only_safe_fields() -> None:
    service, runner, _ = _service(
        _result(
            'os_name="Ubuntu Linux"\n'
            'os_version="24.04"\n'
            "kernel=6.8.0-31-generic\n"
            "architecture=x86_64\n"
        )
    )
    report = service.get_system_info(_profile())
    assert report.check.status is CheckStatus.PASS
    assert (report.os_name, report.os_version, report.kernel, report.architecture) == (
        "Ubuntu Linux",
        "24.04",
        "6.8.0-31-generic",
        "x86_64",
    )
    command = runner.calls[0][0][-1]
    assert "/etc/os-release" in command
    assert "uname -r" in command
    assert "uname -m" in command
    assert "hostname" not in command
    assert "server" not in command


@pytest.mark.parametrize(
    "output",
    [
        "os_name=Ubuntu\nos_version=24.04\nkernel=6.8\n",
        "os_name=Ubuntu\nos_name=Debian\nos_version=12\nkernel=6.1\narchitecture=x86_64\n",
        "os_name=Ubuntu\nos_version=24.04\nkernel=bad value\narchitecture=x86_64\n",
    ],
)
def test_system_info_rejects_missing_or_abnormal_output(output: str) -> None:
    service, _, _ = _service(_result(output))
    report = service.get_system_info(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_SYSTEM_INFO_INVALID"
    assert report.os_name is None


def test_memory_normal_values_are_reduced_to_metrics() -> None:
    service, runner, _ = _service(
        _result("MemTotal:       100000 kB\nMemAvailable:    25000 kB\n")
    )
    report = service.check_memory(_profile())
    assert report.check.status is CheckStatus.PASS
    assert report.total_bytes == 100000 * 1024
    assert report.available_bytes == 25000 * 1024
    assert report.available_percent == 25.0
    assert runner.calls[0][0][-1] == "awk '/^(MemTotal|MemAvailable):/ { print }' /proc/meminfo"


def test_memory_below_threshold_is_pressure() -> None:
    service, _, _ = _service(
        _result("MemTotal:       100000 kB\nMemAvailable:     4990 kB\n")
    )
    report = service.check_memory(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_MEMORY_PRESSURE"
    assert report.available_percent == 4.99


def test_memory_at_five_percent_boundary_passes() -> None:
    service, _, _ = _service(
        _result("MemTotal:       100000 kB\nMemAvailable:     5000 kB\n")
    )
    report = service.check_memory(_profile())
    assert report.check.status is CheckStatus.PASS
    assert report.available_percent == 5.0
    assert not (
        report.available_percent == service.memory_available_threshold
        and report.check.status is CheckStatus.FAIL
    )


@pytest.mark.parametrize(
    "output",
    [
        "MemTotal: invalid kB\nMemAvailable: 1 kB\n",
        "MemTotal: 0 kB\nMemAvailable: 0 kB\n",
        "MemTotal: 10 kB\nMemAvailable: 11 kB\n",
        "MemTotal: 10 kB\n",
    ],
)
def test_memory_rejects_invalid_values(output: str) -> None:
    service, _, _ = _service(_result(output))
    report = service.check_memory(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_MEMORY_INFO_INVALID"
    assert report.total_bytes is None


def test_disk_normal_values_are_reduced_to_root_metrics() -> None:
    service, runner, _ = _service(
        _result("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100000 50000 40000 56% /\n")
    )
    report = service.check_disk(_profile())
    assert report.check.status is CheckStatus.PASS
    assert report.total_bytes == 100000 * 1024
    assert report.used_bytes == 50000 * 1024
    assert report.available_bytes == 40000 * 1024
    assert report.used_percent == 56.0
    assert runner.calls[0][0][-1] == "df -Pk -- /"


@pytest.mark.parametrize("capacity", [95, 100])
def test_disk_at_or_above_ninety_five_percent_is_pressure(capacity: int) -> None:
    output = f"Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100000 50000 40000 {capacity}% /\n"
    service, _, _ = _service(_result(output))
    report = service.check_disk(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_DISK_PRESSURE"
    assert report.used_percent == float(capacity)
    assert not (
        report.used_percent >= service.disk_used_threshold
        and report.check.status is CheckStatus.PASS
    )


def test_disk_below_ninety_five_percent_boundary_passes() -> None:
    service, _, _ = _service(
        _result("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100000 50000 40000 94% /\n")
    )
    report = service.check_disk(_profile())
    assert report.check.status is CheckStatus.PASS
    assert report.used_percent == 94.0


@pytest.mark.parametrize(
    "output",
    [
        "invalid",
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 bad 1 1 50% /\n",
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 0 0 0 0% /\n",
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 10 9 9 90% /\n",
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 10 5 5 50% /tmp\n",
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 10 5 5 abc% /\n",
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 10 5 5 101% /\n",
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 10 5 5 50 /\n",
    ],
)
def test_disk_rejects_invalid_or_non_root_output(output: str) -> None:
    service, _, _ = _service(_result(output))
    report = service.check_disk(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_DISK_INFO_INVALID"


@pytest.mark.parametrize("method", ["get_system_info", "check_memory", "check_disk"])
def test_ssh_unavailable_is_safe_and_does_not_expose_stderr(method: str) -> None:
    service, _, _ = _service(
        _result(
            exit_code=255,
            stderr="password=RAB-SECRET C:\\Users\\private",
            error_code="PROCESS_EXIT_NONZERO",
        )
    )
    report = getattr(service, method)(_profile())
    assert report.check.error_code == "REMOTE_SYSTEM_UNAVAILABLE"
    assert "RAB-SECRET" not in report.check.detail
    assert "Users" not in report.check.detail


def test_ssh_timeout_is_safely_classified() -> None:
    service, _, _ = _service(
        _result(
            exit_code=None,
            stderr="secret timeout detail",
            error_code="PROCESS_TIMEOUT",
            timed_out=True,
        )
    )
    report = service.check_memory(_profile())
    assert report.check.error_code == "REMOTE_SYSTEM_UNAVAILABLE"
    assert report.check.detail == "remote system check timed out"


def test_legacy_profile_uses_existing_trusted_ssh_target_without_guessing() -> None:
    service, runner, probe = _service(
        _result(
            "os_name=Debian\nos_version=12\nkernel=6.1.0\narchitecture=x86_64\n"
        )
    )
    report = service.get_system_info(_profile(legacy=True))
    assert report.check.status is CheckStatus.PASS
    assert probe.profiles[0].ssh_target == "trusted-alias"
    assert runner.calls[0][0][-2] == "trusted-alias"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", 0),
        ("timeout", 61),
        ("timeout", True),
        ("memory_available_threshold", -1),
        ("memory_available_threshold", 101),
        ("disk_used_threshold", -1),
        ("disk_used_threshold", 101),
    ],
)
def test_service_configuration_is_bounded(field: str, value: object) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError):
        _service(_result(), **kwargs)


def test_reports_are_json_safe_and_deterministic() -> None:
    service, _, _ = _service(
        _result("MemTotal:       100000 kB\nMemAvailable:    25000 kB\n")
    )
    report = service.check_memory(_profile())
    payload = {
        "check": {
            "name": report.check.name,
            "status": report.check.status.value,
            "detail": report.check.detail,
            "error_code": report.check.error_code,
        },
        "total_bytes": report.total_bytes,
        "available_bytes": report.available_bytes,
        "available_percent": report.available_percent,
    }
    assert json.dumps(payload, sort_keys=True) == json.dumps(payload, sort_keys=True)
