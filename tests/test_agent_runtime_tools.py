from __future__ import annotations

import json

import pytest

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.services.agent.runtime_tools import build_runtime_tools
from app.services.agent.tool_registry import ToolRegistry
from app.services.remote_runtime import (
    RemoteLoadReport,
    RemoteProcessEntry,
    RemoteProcessSnapshot,
    RemoteServiceReport,
)


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


class FakeProfiles:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, name: str) -> Profile:
        self.calls.append(name)
        return _profile()


class FakeRemoteRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def check_load(self, profile: Profile) -> RemoteLoadReport:
        self.calls.append("load")
        return RemoteLoadReport(
            CheckResult(
                "Remote load",
                CheckStatus.FAIL,
                "load pressure",
                "REMOTE_LOAD_PRESSURE",
            ),
            8.0,
            4.0,
            2.0,
            4,
            2.0,
        )

    def get_top_processes(self, profile: Profile) -> RemoteProcessSnapshot:
        self.calls.append("processes")
        return RemoteProcessSnapshot(
            CheckResult("Remote process snapshot", CheckStatus.PASS, "safe"),
            (RemoteProcessEntry("python", 250.0, 10.0),),
        )

    def check_failed_services(self, profile: Profile) -> RemoteServiceReport:
        self.calls.append("services")
        return RemoteServiceReport(
            CheckResult(
                "Remote failed services",
                CheckStatus.FAIL,
                "failed services",
                "REMOTE_FAILED_SERVICES",
            ),
            ("nginx.service",),
            False,
        )


def _registry():
    profiles = FakeProfiles()
    runtime = FakeRemoteRuntime()
    registry = ToolRegistry()
    for tool in build_runtime_tools(profiles, runtime):
        registry.register(tool)
    return registry, profiles, runtime


def test_runtime_tool_contracts_are_stable_read_only_system_tools() -> None:
    registry, _, _ = _registry()
    definitions = registry.list_definitions()
    assert [item.name for item in definitions] == [
        "check_remote_failed_services",
        "check_remote_load",
        "get_remote_top_processes",
    ]
    assert {item.category for item in definitions} == {"system"}
    assert {item.risk_level for item in definitions} == {"read_only"}


def test_runtime_tools_only_accept_profile_name() -> None:
    registry, _, _ = _registry()
    for definition in registry.list_definitions():
        assert set(definition.input_schema["properties"]) == {"profile_name"}
        assert definition.input_schema["required"] == ["profile_name"]
        assert definition.input_schema["additionalProperties"] is False
        for forbidden in (
            "command",
            "pid",
            "process_name",
            "service_name",
            "sort",
            "limit",
            "path",
            "host",
            "port",
        ):
            assert forbidden not in definition.input_schema["properties"]


@pytest.mark.parametrize(
    "tool_name",
    ["check_remote_load", "get_remote_top_processes", "check_remote_failed_services"],
)
@pytest.mark.parametrize(
    "extra",
    [
        {"command": "cat /etc/shadow"},
        {"pid": 1},
        {"service_name": "sshd"},
        {"path": "/var/log/auth.log"},
        {"limit": 1000},
        {"host": "scan.example", "port": 22},
    ],
)
def test_runtime_tools_reject_extra_arguments(
    tool_name: str,
    extra: dict[str, object],
) -> None:
    registry, profiles, runtime = _registry()
    result = registry.execute(tool_name, {"profile_name": "server", **extra})
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert profiles.calls == []
    assert runtime.calls == []


@pytest.mark.parametrize(
    ("tool_name", "evidence_id", "metric_names"),
    [
        (
            "check_remote_load",
            "system.remote.load",
            {"load_1m", "load_5m", "load_15m", "cpu_count", "load_1m_per_cpu"},
        ),
        (
            "get_remote_top_processes",
            "system.remote.processes",
            {"processes"},
        ),
        (
            "check_remote_failed_services",
            "system.remote.services",
            {"failed_services", "truncated"},
        ),
    ],
)
def test_runtime_tools_return_shared_evidence_and_minimal_metrics(
    tool_name: str,
    evidence_id: str,
    metric_names: set[str],
) -> None:
    registry, _, _ = _registry()
    result = registry.execute(tool_name, {"profile_name": "server"})
    assert result.ok is True
    evidence = result.data["evidence"][0]
    assert evidence["id"] == evidence_id
    assert evidence["category"] == "system"
    assert set(result.data["metrics"]) == metric_names


def test_process_tool_exposes_only_safe_process_fields() -> None:
    registry, _, _ = _registry()
    result = registry.execute("get_remote_top_processes", {"profile_name": "server"})
    process = result.data["metrics"]["processes"][0]
    assert process == {"name": "python", "cpu_percent": 250.0, "memory_percent": 10.0}
    assert {"pid", "user", "args", "environment"}.isdisjoint(process)


def test_runtime_tools_call_the_expected_single_service_method() -> None:
    registry, profiles, runtime = _registry()
    registry.execute("check_remote_load", {"profile_name": "server"})
    registry.execute("get_remote_top_processes", {"profile_name": "server"})
    registry.execute("check_remote_failed_services", {"profile_name": "server"})
    assert profiles.calls == ["server", "server", "server"]
    assert runtime.calls == ["load", "processes", "services"]


def test_runtime_tool_results_exclude_credentials_commands_and_raw_output() -> None:
    registry, _, _ = _registry()
    outputs = [
        registry.execute(item.name, {"profile_name": "server"}).to_dict()
        for item in registry.list_definitions()
    ]
    serialized = json.dumps(outputs, sort_keys=True)
    for forbidden in (
        "alice",
        "ssh.example.test",
        "11111111111111111111111111111111",
        "SHA256:",
        "/proc/loadavg",
        "systemctl",
        "ps -eo",
    ):
        assert forbidden not in serialized


def test_malicious_profile_name_never_reaches_runtime_service() -> None:
    registry, profiles, runtime = _registry()
    result = registry.execute(
        "check_remote_load",
        {"profile_name": "server; uname -a"},
    )
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert profiles.calls == []
    assert runtime.calls == []


def test_runtime_tool_output_is_json_safe_and_deterministic() -> None:
    first, _, _ = _registry()
    second, _, _ = _registry()
    first_payload = [
        first.execute(item.name, {"profile_name": "server"}).to_dict()
        for item in first.list_definitions()
    ]
    second_payload = [
        second.execute(item.name, {"profile_name": "server"}).to_dict()
        for item in second.list_definitions()
    ]
    assert json.dumps(first_payload, sort_keys=True, allow_nan=False) == json.dumps(
        second_payload,
        sort_keys=True,
        allow_nan=False,
    )
