from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.domain.supervisor import SupervisorSnapshot, SupervisorState, utc_now
from app.services.agent.connection_tools import build_connection_tools
from app.services.agent.tool_registry import ToolRegistry
from app.services.doctor import DoctorReport
from app.services.local_proxy import LocalProxyReport


def _profile(**changes) -> Profile:
    base = Profile(
        schema_version=1,
        name="server",
        ssh_target="example-server",
        local_proxy_port=7897,
        remote_port=17890,
    )
    return replace(base, **changes)


def _check(name: str, detail: str = "healthy") -> CheckResult:
    return CheckResult(name, CheckStatus.PASS, detail)


def _doctor_report(secret: bool = False, status: CheckStatus = CheckStatus.PASS) -> DoctorReport:
    detail = "password=RAB-SECRET" if secret else "healthy"
    first = CheckResult("TCP", status, detail, "CHECK_FAILED" if status is CheckStatus.FAIL else None)
    return DoctorReport(
        LocalProxyReport(first, _check("Handshake"), _check("Endpoint")),
        _check("SSH"),
        _check("Tunnel"),
        _check("Remote listener"),
        _check("Remote endpoint"),
    )


class FakeRuntime:
    def __init__(self) -> None:
        self.names: list[str] = []

    def status(self, name: str) -> SupervisorSnapshot:
        self.names.append(name)
        return SupervisorSnapshot(
            name,
            SupervisorState.READY,
            utc_now(),
            attempt=2,
            message="password=RAB-SECRET",
            pid=456,
            tunnel_id="private-tunnel-id",
            remote_port=17890,
            supervised=True,
            runtime_present=True,
            process_alive=True,
        )


class FakeProfiles:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.names: list[str] = []

    def get(self, name: str) -> Profile:
        self.names.append(name)
        return self.profile


class FakeDoctor:
    def __init__(self, report: DoctorReport) -> None:
        self.report = report
        self.profiles: list[Profile] = []

    def run(self, profile: Profile) -> DoctorReport:
        self.profiles.append(profile)
        return self.report


def _registry(*, secret_doctor: bool = False):
    runtime = FakeRuntime()
    profiles = FakeProfiles(_profile())
    doctor = FakeDoctor(_doctor_report(secret_doctor))
    registry = ToolRegistry()
    for tool in build_connection_tools(runtime, profiles, doctor):
        registry.register(tool)
    return registry, runtime, profiles, doctor


def test_connection_tool_names_are_exact() -> None:
    registry, *_ = _registry()
    assert [item.name for item in registry.list_definitions()] == [
        "get_profile_summary",
        "get_runtime_status",
        "run_doctor",
    ]


def test_all_connection_tools_are_read_only() -> None:
    registry, *_ = _registry()
    assert {(item.category, item.risk_level) for item in registry.list_definitions()} == {
        ("connection", "read_only")
    }


def test_runtime_tool_calls_runtime_manager_status() -> None:
    registry, runtime, *_ = _registry()
    registry.execute("get_runtime_status", {"profile_name": "server"})
    assert runtime.names == ["server"]


def test_runtime_tool_returns_safe_diagnostic_summary() -> None:
    registry, *_ = _registry()
    result = registry.execute("get_runtime_status", {"profile_name": "server"})
    assert result.ok and result.data["state"] == "READY"
    assert result.data["remote_port"] == 17890


@pytest.mark.parametrize("forbidden", ["pid", "tunnel_id", "profile_name", "updated_at"])
def test_runtime_tool_excludes_identity_fields(forbidden: str) -> None:
    registry, *_ = _registry()
    result = registry.execute("get_runtime_status", {"profile_name": "server"})
    assert forbidden not in result.data


def test_runtime_tool_redacts_message() -> None:
    registry, *_ = _registry()
    result = registry.execute("get_runtime_status", {"profile_name": "server"})
    assert "RAB-SECRET" not in result.message + str(result.data)


def test_doctor_tool_loads_profile_then_runs_doctor() -> None:
    registry, _, profiles, doctor = _registry()
    registry.execute("run_doctor", {"profile_name": "server"})
    assert profiles.names == ["server"]
    assert doctor.profiles == [profiles.profile]


def test_doctor_tool_returns_canonical_evidence_ids_in_order() -> None:
    registry, *_ = _registry()
    result = registry.execute("run_doctor", {"profile_name": "server"})
    assert [item["id"] for item in result.data["evidence"]] == [
        "connection.proxy.tcp",
        "connection.proxy.handshake",
        "connection.proxy.endpoint",
        "connection.ssh.config",
        "connection.tunnel.process",
        "connection.remote.listener",
        "connection.remote.endpoint",
    ]


def test_doctor_tool_evidence_uses_connection_category() -> None:
    registry, *_ = _registry()
    result = registry.execute("run_doctor", {"profile_name": "server"})
    assert {item["category"] for item in result.data["evidence"]} == {"connection"}


@pytest.mark.parametrize("status", [CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.SKIP])
def test_doctor_tool_preserves_check_status(status: CheckStatus) -> None:
    runtime = FakeRuntime()
    profiles = FakeProfiles(_profile())
    doctor = FakeDoctor(_doctor_report(status=status))
    registry = ToolRegistry()
    for tool in build_connection_tools(runtime, profiles, doctor):
        registry.register(tool)
    result = registry.execute("run_doctor", {"profile_name": "server"})
    assert result.data["evidence"][0]["status"] == status.value


def test_doctor_tool_redacts_evidence_details() -> None:
    registry, *_ = _registry(secret_doctor=True)
    result = registry.execute("run_doctor", {"profile_name": "server"})
    assert "RAB-SECRET" not in str(result.data)


def test_profile_summary_calls_profile_service() -> None:
    registry, _, profiles, _ = _registry()
    registry.execute("get_profile_summary", {"profile_name": "server"})
    assert profiles.names == ["server"]


def test_profile_summary_matches_safe_diagnostic_fields() -> None:
    registry, *_ = _registry()
    result = registry.execute("get_profile_summary", {"profile_name": "server"})
    assert set(result.data) == {
        "name",
        "profile_type",
        "local_proxy_host",
        "local_proxy_port",
        "remote_bind_host",
        "remote_port",
        "auto_reconnect",
        "endpoint_probe_url",
    }


@pytest.mark.parametrize(
    "forbidden",
    ["ssh_target", "host", "username", "ssh_port", "key_id", "host_key_fingerprint", "credential"],
)
def test_profile_summary_excludes_credentials_and_ssh_identity(forbidden: str) -> None:
    registry, *_ = _registry()
    result = registry.execute("get_profile_summary", {"profile_name": "server"})
    assert forbidden not in result.data


def test_invalid_profile_name_causes_no_service_calls() -> None:
    registry, runtime, profiles, doctor = _registry()
    result = registry.execute("run_doctor", {"profile_name": "server; rm -rf"})
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert runtime.names == [] and profiles.names == [] and doctor.profiles == []


def test_same_input_produces_same_tool_result_structure() -> None:
    registry, *_ = _registry()
    first = registry.execute("get_profile_summary", {"profile_name": "server"}).to_dict()
    second = registry.execute("get_profile_summary", {"profile_name": "server"}).to_dict()
    assert first == second


def test_connection_tools_do_not_import_process_execution_modules() -> None:
    source = Path("app/services/agent/connection_tools.py").read_text(encoding="utf-8").lower()
    for forbidden in ("subprocess", "processrunner", "shell=true", "os.system", "terminate", "kill"):
        assert forbidden not in source


def test_connection_tools_do_not_invoke_ai_inference() -> None:
    source = Path("app/services/agent/connection_tools.py").read_text(encoding="utf-8")
    assert "DiagnosisProvider" not in source
    assert "AIDiagnosticService" not in source
