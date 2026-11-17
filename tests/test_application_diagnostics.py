from __future__ import annotations

import json

import pytest

from app.domain.application_target import (
    ApplicationTarget,
    ApplicationTargetValidationError,
)
from app.domain.health import CheckStatus
from app.domain.profile import Profile
from app.infrastructure.process_runner import ProcessResult
from app.services.application_diagnostics import ApplicationDiagnosticsService


def _profile(name: str = "server") -> Profile:
    return Profile(
        schema_version=2,
        name=name,
        ssh_target="alice@ssh.example.test",
        profile_type="managed",
        host="ssh.example.test",
        username="alice",
        ssh_port=2222,
        key_id="1" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:" + "A" * 43,
    )


def _target(**changes) -> ApplicationTarget:
    values = {
        "schema_version": 1,
        "name": "rab-api",
        "profile_name": "server",
        "service_unit": "rab-api.service",
        "health_url": "http://127.0.0.1:8000/health",
    }
    values.update(changes)
    return ApplicationTarget(**values)


def _result(
    stdout: str = "",
    *,
    exit_code: int | None = 0,
    stderr: str = "",
    error_code: str | None = None,
    timed_out: bool = False,
) -> ProcessResult:
    return ProcessResult(("ssh.exe",), exit_code, stdout, stderr, error_code, timed_out)


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


def _service(result: ProcessResult, **kwargs):
    runner = FakeRunner(result)
    probe = FakeRemoteProbe()
    return ApplicationDiagnosticsService(runner, probe, **kwargs), runner, probe


def _service_output(
    load: str = "loaded",
    active: str = "active",
    sub: str = "running",
) -> str:
    return f"LoadState={load}\nActiveState={active}\nSubState={sub}\n"


def test_active_running_application_service_is_pass() -> None:
    service, runner, probe = _service(_result(_service_output()))
    report = service.check_service(_profile(), _target())
    assert report.check.status is CheckStatus.PASS
    assert (report.active_state, report.sub_state) == ("active", "running")
    assert probe.profiles == [_profile()]
    command = runner.calls[0][0][-1]
    assert "LC_ALL=C systemctl show" in command
    assert command.endswith("-- rab-api.service")
    assert "journalctl" not in command


@pytest.mark.parametrize("active_state", ["inactive", "failed"])
def test_inactive_or_failed_application_service_is_failure(active_state: str) -> None:
    service, _, _ = _service(
        _result(_service_output(active=active_state, sub="dead"))
    )
    report = service.check_service(_profile(), _target())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "APPLICATION_SERVICE_INACTIVE"
    assert report.active_state == active_state


def test_application_service_not_found_is_distinct_failure() -> None:
    service, _, _ = _service(
        _result(_service_output(load="not-found", active="inactive", sub="dead"))
    )
    report = service.check_service(_profile(), _target())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "APPLICATION_SERVICE_NOT_FOUND"


def test_application_service_manager_unavailable_is_skip_without_raw_error() -> None:
    service, _, _ = _service(
        _result(
            exit_code=127,
            stderr="password=RAB-SECRET /home/alice",
            error_code="PROCESS_EXIT_NONZERO",
        )
    )
    report = service.check_service(_profile(), _target())
    assert report.check.status is CheckStatus.SKIP
    assert report.check.error_code == "APPLICATION_SERVICE_MANAGER_UNAVAILABLE"
    assert "RAB-SECRET" not in report.check.detail
    assert "/home" not in report.check.detail


@pytest.mark.parametrize(
    "output",
    [
        "LoadState=loaded\nActiveState=active\n",
        "LoadState=loaded\nActiveState=active\nSubState=running\nExtra=x\n",
        "LoadState=loaded\nActiveState=active\nSubState=bad state\n",
        "LoadState=loaded\nLoadState=loaded\nActiveState=active\nSubState=running\n",
    ],
)
def test_malformed_application_service_status_is_rejected(output: str) -> None:
    service, _, _ = _service(_result(output))
    report = service.check_service(_profile(), _target())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "APPLICATION_SERVICE_STATUS_INVALID"
    assert report.active_state is None


@pytest.mark.parametrize(
    ("result", "expected_code"),
    [
        (
            _result(
                exit_code=255,
                stderr="token=RAB-SECRET command detail",
                error_code="PROCESS_EXIT_NONZERO",
            ),
            "APPLICATION_SERVICE_STATUS_UNAVAILABLE",
        ),
        (
            _result(
                exit_code=None,
                stderr="token=RAB-SECRET command detail",
                error_code="PROCESS_TIMEOUT",
                timed_out=True,
            ),
            "APPLICATION_SERVICE_STATUS_UNAVAILABLE",
        ),
    ],
)
def test_application_service_transport_failure_is_safe(
    result: ProcessResult,
    expected_code: str,
) -> None:
    service, _, _ = _service(result)
    report = service.check_service(_profile(), _target())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == expected_code
    serialized = json.dumps(report.check.__dict__)
    assert "RAB-SECRET" not in serialized
    assert "command detail" not in serialized


def test_application_service_not_configured_skips_without_remote_execution() -> None:
    service, runner, probe = _service(_result())
    report = service.check_service(_profile(), _target(service_unit=None))
    assert report.check.status is CheckStatus.SKIP
    assert report.check.error_code == "APPLICATION_SERVICE_NOT_CONFIGURED"
    assert runner.calls == []
    assert probe.profiles == []


@pytest.mark.parametrize("status", [200, 204])
def test_healthy_application_endpoint_is_pass(status: int) -> None:
    service, runner, _ = _service(_result(str(status)))
    report = service.check_health(_profile(), _target())
    assert report.check.status is CheckStatus.PASS
    assert report.http_status == status
    assert report.check.http_status == status
    command = runner.calls[0][0][-1]
    assert "--noproxy '*'" in command
    assert "--globoff" in command
    assert "--location" not in command
    assert " -L " not in f" {command} "
    assert "--output /dev/null" in command


@pytest.mark.parametrize("status", [302, 400, 500])
def test_non_2xx_application_endpoint_is_unhealthy_without_redirect(status: int) -> None:
    service, runner, _ = _service(_result(str(status)))
    report = service.check_health(_profile(), _target())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "APPLICATION_HEALTH_UNHEALTHY"
    assert report.http_status == status
    command = runner.calls[0][0][-1]
    assert "--location" not in command
    assert " -L " not in f" {command} "


def test_application_health_timeout_is_safe() -> None:
    service, _, _ = _service(
        _result(
            exit_code=None,
            stderr="password=RAB-SECRET",
            error_code="PROCESS_TIMEOUT",
            timed_out=True,
        )
    )
    report = service.check_health(_profile(), _target())
    assert report.check.error_code == "APPLICATION_HEALTH_TIMEOUT"
    assert "RAB-SECRET" not in report.check.detail


def test_curl_exit_28_is_classified_as_timeout() -> None:
    service, _, _ = _service(
        _result(exit_code=28, stderr="private details", error_code="PROCESS_EXIT_NONZERO")
    )
    report = service.check_health(_profile(), _target())
    assert report.check.error_code == "APPLICATION_HEALTH_TIMEOUT"
    assert "private details" not in report.check.detail


def test_application_health_connection_failure_is_safe() -> None:
    service, _, _ = _service(
        _result(
            exit_code=7,
            stdout="response body RAB-BODY",
            stderr="Authorization: Bearer RAB-TOKEN",
            error_code="PROCESS_EXIT_NONZERO",
        )
    )
    report = service.check_health(_profile(), _target())
    assert report.check.error_code == "APPLICATION_HEALTH_UNREACHABLE"
    serialized = json.dumps(report.check.__dict__)
    assert "RAB-BODY" not in serialized
    assert "RAB-TOKEN" not in serialized


@pytest.mark.parametrize("output", ["", "abc", "20", "200 body", "000", "600"])
def test_invalid_application_health_status_is_rejected(output: str) -> None:
    service, _, _ = _service(_result(output))
    report = service.check_health(_profile(), _target())
    assert report.check.status is CheckStatus.FAIL
    assert report.check.error_code == "APPLICATION_HEALTH_INVALID_RESPONSE"
    assert report.http_status is None


def test_application_health_not_configured_skips_without_remote_execution() -> None:
    service, runner, probe = _service(_result())
    report = service.check_health(_profile(), _target(health_url=None))
    assert report.check.status is CheckStatus.SKIP
    assert report.check.error_code == "APPLICATION_HEALTH_NOT_CONFIGURED"
    assert runner.calls == []
    assert probe.profiles == []


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254:80/",
        "http://example.com:80/",
        "http://10.0.0.1:8000/",
        "http://user:pass@127.0.0.1:8000/",
    ],
)
def test_invalid_health_target_never_executes_remote_curl(url: str) -> None:
    service, runner, probe = _service(_result("200"))
    with pytest.raises(ApplicationTargetValidationError):
        service.check_health(_profile(), _target(health_url=url))
    assert runner.calls == []
    assert probe.profiles == []


def test_profile_target_mismatch_never_executes_remote_command() -> None:
    service, runner, probe = _service(_result("200"))
    with pytest.raises(ValueError, match="does not belong"):
        service.check_health(_profile("other"), _target())
    assert runner.calls == []
    assert probe.profiles == []


@pytest.mark.parametrize("timeout", [0, 61, True, "15", float("nan")])
def test_application_diagnostics_timeout_is_bounded(timeout: object) -> None:
    with pytest.raises(ValueError):
        _service(_result(), timeout=timeout)
