from __future__ import annotations

import json

import pytest

from app.domain.health import CheckStatus
from app.domain.profile import Profile
from app.infrastructure.process_runner import ProcessResult
from app.services.remote_runtime import RemoteRuntimeService


def _profile() -> Profile:
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
    return RemoteRuntimeService(runner, probe, **kwargs), runner, probe


def test_load_normalizes_per_cpu_metric() -> None:
    service, runner, _ = _service(_result("0.50 0.40 0.30 1/100 1\n4\n"))
    report = service.check_load(_profile())
    assert report.check.status is CheckStatus.PASS
    assert (report.load_1m, report.load_5m, report.load_15m) == (0.5, 0.4, 0.3)
    assert report.cpu_count == 4
    assert report.load_1m_per_cpu == 0.125
    command = runner.calls[0][0][-1]
    assert "/proc/loadavg" in command
    assert "getconf _NPROCESSORS_ONLN" in command
    assert "server" not in command


def test_load_at_two_per_cpu_is_pressure() -> None:
    service, _, _ = _service(_result("8.0 1.0 0.5 1/100 1\n4\n"))
    report = service.check_load(_profile())
    assert report.load_1m_per_cpu == 2.0
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_LOAD_PRESSURE"


@pytest.mark.parametrize(
    ("load", "expected_status", "expected_metric"),
    [
        ("7.96", CheckStatus.PASS, 1.99),
        ("8.00", CheckStatus.FAIL, 2.0),
    ],
)
def test_load_threshold_uses_exposed_rounded_metric(
    load: str,
    expected_status: CheckStatus,
    expected_metric: float,
) -> None:
    service, _, _ = _service(_result(f"{load} 1.0 0.5 1/100 1\n4\n"))
    report = service.check_load(_profile())
    assert report.load_1m_per_cpu == expected_metric
    assert report.check.status is expected_status


@pytest.mark.parametrize(
    "output",
    [
        "0.5 0.4\n4\n",
        "bad 0.4 0.3 1/1 1\n4\n",
        "nan 0.4 0.3 1/1 1\n4\n",
        "inf 0.4 0.3 1/1 1\n4\n",
        "-0.1 0.4 0.3 1/1 1\n4\n",
        "0.5 0.4 0.3 1/1 1\n0\n",
        "0.5 0.4 0.3 1/1 1\nbad\n",
        "0.5 0.4 0.3 1/1 1\n4\nextra\n",
    ],
)
def test_invalid_load_output_is_rejected(output: str) -> None:
    service, _, _ = _service(_result(output))
    report = service.check_load(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_LOAD_INFO_INVALID"
    assert report.load_1m is None


def test_process_snapshot_accepts_five_safe_entries_and_multicore_cpu() -> None:
    output = "\n".join(
        [
            "python 250.5 20.0",
            "postgres 80.0 10.5",
            "kworker/0:1 12.0 0.0",
            "sshd 2.5 0.2",
            "systemd 1.0 0.1",
        ]
    )
    service, runner, _ = _service(_result(output))
    report = service.get_top_processes(_profile())
    assert report.check.status is CheckStatus.PASS
    assert len(report.processes) == 5
    assert report.processes[0].cpu_percent == 250.5
    assert report.processes[0].to_dict() == {
        "name": "python",
        "cpu_percent": 250.5,
        "memory_percent": 20.0,
    }
    command = runner.calls[0][0][-1]
    assert "comm=" in command
    assert "args" not in command
    assert "head -n 5" in command


def test_process_snapshot_accepts_fewer_than_five_and_empty_output() -> None:
    service, _, _ = _service(_result("sshd 1.0 0.2\n"))
    assert len(service.get_top_processes(_profile()).processes) == 1
    empty_service, _, _ = _service(_result("\n"))
    empty = empty_service.get_top_processes(_profile())
    assert empty.check.status is CheckStatus.PASS
    assert empty.processes == ()


@pytest.mark.parametrize(
    ("output", "expected_name"),
    [
        ("worker thread 10.0 1.0", "worker thread"),
        ("Web Content 25.0 2.0", "Web Content"),
    ],
)
def test_process_snapshot_accepts_safe_ascii_spaces_in_name(
    output: str,
    expected_name: str,
) -> None:
    service, runner, _ = _service(_result(output))
    report = service.get_top_processes(_profile())
    assert report.check.status is CheckStatus.PASS
    assert report.processes[0].name == expected_name
    assert "LC_ALL=C ps" in runner.calls[0][0][-1]


@pytest.mark.parametrize(
    "output",
    [
        "python bad 1.0",
        "python nan 1.0",
        "python -1.0 1.0",
        "python 1.0 bad",
        "python 1.0 101.0",
        "python 1.0 -0.1",
        f"{'a' * 65} 1.0 1.0",
        "bad\x01name 1.0 1.0",
        "worker\tthread 1.0 1.0",
        "worker\nthread 1.0 1.0",
    ],
)
def test_invalid_process_snapshot_is_rejected(output: str) -> None:
    service, _, _ = _service(_result(output))
    report = service.get_top_processes(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_PROCESS_SNAPSHOT_INVALID"
    assert report.processes == ()


def test_process_snapshot_never_returns_pid_user_or_arguments() -> None:
    service, _, _ = _service(_result("python 25.0 5.0\n"))
    payload = service.get_top_processes(_profile()).processes[0].to_dict()
    assert set(payload) == {"name", "cpu_percent", "memory_percent"}
    assert {"pid", "user", "args", "command"}.isdisjoint(payload)


def test_zero_failed_services_is_pass() -> None:
    service, runner, _ = _service(_result(""))
    report = service.check_failed_services(_profile())
    assert report.check.status is CheckStatus.PASS
    assert report.failed_services == ()
    assert report.truncated is False
    command = runner.calls[0][0][-1]
    assert "systemctl --failed --type=service" in command
    assert "journalctl" not in command


def test_one_failed_service_is_failure() -> None:
    service, _, _ = _service(_result("nginx.service\n"))
    report = service.check_failed_services(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_FAILED_SERVICES"
    assert report.failed_services == ("nginx.service",)


def test_multiple_failed_services_are_preserved() -> None:
    service, _, _ = _service(_result("nginx.service\ndocker.service\n"))
    report = service.check_failed_services(_profile())
    assert report.failed_services == ("nginx.service", "docker.service")
    assert report.truncated is False


def test_failed_services_are_limited_to_twenty() -> None:
    output = "\n".join(f"service-{index}.service" for index in range(21))
    service, _, _ = _service(_result(output))
    report = service.check_failed_services(_profile())
    assert len(report.failed_services) == 20
    assert report.failed_services[0] == "service-0.service"
    assert report.failed_services[-1] == "service-19.service"
    assert report.truncated is True


@pytest.mark.parametrize(
    "output",
    [
        "nginx",
        "bad service.service",
        "../../evil.service",
        "bad\x01.service",
        "nginx.service\nnginx.service\n",
        f"{'a' * 248}.service",
    ],
)
def test_malformed_failed_service_output_is_rejected(output: str) -> None:
    service, _, _ = _service(_result(output))
    report = service.check_failed_services(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_SERVICE_INFO_INVALID"
    assert report.failed_services == ()


@pytest.mark.parametrize("exit_code", [1, 127])
def test_unavailable_service_manager_is_skip(exit_code: int) -> None:
    service, _, _ = _service(
        _result(
            exit_code=exit_code,
            stderr="password=RAB-SECRET service manager detail",
            error_code="PROCESS_EXIT_NONZERO",
        )
    )
    report = service.check_failed_services(_profile())
    assert report.check.status is CheckStatus.SKIP
    assert report.check.error_code == "REMOTE_SERVICE_MANAGER_UNAVAILABLE"
    assert "RAB-SECRET" not in report.check.detail


@pytest.mark.parametrize("method", ["check_load", "get_top_processes"])
def test_remote_transport_failure_is_safe(method: str) -> None:
    service, _, _ = _service(
        _result(
            exit_code=255,
            stderr="token=RAB-SECRET /home/private",
            error_code="PROCESS_EXIT_NONZERO",
        )
    )
    report = getattr(service, method)(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert "RAB-SECRET" not in report.check.detail
    assert "private" not in report.check.detail


def test_service_check_ssh_timeout_is_system_unavailable() -> None:
    service, _, _ = _service(
        _result(
            exit_code=None,
            stderr="secret",
            error_code="PROCESS_TIMEOUT",
            timed_out=True,
        )
    )
    report = service.check_failed_services(_profile())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "REMOTE_SYSTEM_UNAVAILABLE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", 0),
        ("timeout", 61),
        ("timeout", float("nan")),
        ("load_per_cpu_threshold", 0),
        ("load_per_cpu_threshold", float("inf")),
        ("load_per_cpu_threshold", True),
    ],
)
def test_runtime_configuration_is_bounded(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _service(_result(), **{field: value})


def test_runtime_reports_are_json_safe_and_deterministic() -> None:
    output = "python 25.0 5.0\nsshd 1.0 0.2\n"
    first, _, _ = _service(_result(output))
    second, _, _ = _service(_result(output))
    first_payload = [item.to_dict() for item in first.get_top_processes(_profile()).processes]
    second_payload = [item.to_dict() for item in second.get_top_processes(_profile()).processes]
    assert json.dumps(first_payload, sort_keys=True, allow_nan=False) == json.dumps(
        second_payload,
        sort_keys=True,
        allow_nan=False,
    )
