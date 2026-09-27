from __future__ import annotations

import json

import pytest

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.services.agent.system_tools import build_system_tools
from app.services.agent.tool_registry import ToolRegistry
from app.services.remote_system import RemoteDiskReport, RemoteMemoryReport, RemoteSystemInfo


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


class FakeRemoteSystem:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_system_info(self, profile: Profile) -> RemoteSystemInfo:
        self.calls.append(("system", profile.name))
        return RemoteSystemInfo(
            CheckResult("Remote system information", CheckStatus.PASS, "safe"),
            "Ubuntu Linux",
            "24.04",
            "6.8.0",
            "x86_64",
        )

    def check_memory(self, profile: Profile) -> RemoteMemoryReport:
        self.calls.append(("memory", profile.name))
        return RemoteMemoryReport(
            CheckResult(
                "Remote memory",
                CheckStatus.FAIL,
                "memory pressure",
                "REMOTE_MEMORY_PRESSURE",
            ),
            1000,
            40,
            4.0,
        )

    def check_disk(self, profile: Profile) -> RemoteDiskReport:
        self.calls.append(("disk", profile.name))
        return RemoteDiskReport(
            CheckResult(
                "Remote root filesystem",
                CheckStatus.FAIL,
                "disk pressure",
                "REMOTE_DISK_PRESSURE",
            ),
            1000,
            950,
            50,
            95.0,
        )


def _registry():
    profiles = FakeProfiles()
    remote = FakeRemoteSystem()
    registry = ToolRegistry()
    for tool in build_system_tools(profiles, remote):
        registry.register(tool)
    return registry, profiles, remote


def test_system_tool_contracts_are_stable_and_read_only() -> None:
    registry, _, _ = _registry()
    definitions = registry.list_definitions()
    assert [item.name for item in definitions] == [
        "check_remote_disk",
        "check_remote_memory",
        "get_remote_system_info",
    ]
    assert {item.category for item in definitions} == {"system"}
    assert {item.risk_level for item in definitions} == {"read_only"}


def test_system_tools_only_accept_profile_name() -> None:
    registry, _, _ = _registry()
    for definition in registry.list_definitions():
        assert set(definition.input_schema["properties"]) == {"profile_name"}
        assert definition.input_schema["required"] == ["profile_name"]
        assert definition.input_schema["additionalProperties"] is False
        for forbidden in ("command", "path", "pid", "host", "port"):
            assert forbidden not in definition.input_schema["properties"]


@pytest.mark.parametrize(
    "tool_name",
    ["get_remote_system_info", "check_remote_memory", "check_remote_disk"],
)
@pytest.mark.parametrize(
    "extra",
    [
        {"command": "cat /etc/shadow"},
        {"path": "/home/alice"},
        {"pid": 1},
    ],
)
def test_system_tools_reject_all_extra_arguments(tool_name: str, extra: dict[str, object]) -> None:
    registry, profiles, remote = _registry()
    result = registry.execute(tool_name, {"profile_name": "server", **extra})
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert profiles.calls == []
    assert remote.calls == []


@pytest.mark.parametrize(
    ("tool_name", "evidence_id", "metric_names"),
    [
        (
            "get_remote_system_info",
            "system.remote.os",
            {"os_name", "os_version", "kernel", "architecture"},
        ),
        (
            "check_remote_memory",
            "system.remote.memory",
            {"total_bytes", "available_bytes", "available_percent"},
        ),
        (
            "check_remote_disk",
            "system.remote.disk",
            {"total_bytes", "used_bytes", "available_bytes", "used_percent"},
        ),
    ],
)
def test_system_tools_return_shared_evidence_and_minimal_metrics(
    tool_name: str,
    evidence_id: str,
    metric_names: set[str],
) -> None:
    registry, _, _ = _registry()
    result = registry.execute(tool_name, {"profile_name": "server"})
    assert result.ok is True
    assert result.data["evidence"][0]["id"] == evidence_id
    assert result.data["evidence"][0]["category"] == "system"
    assert set(result.data["metrics"]) == metric_names


def test_each_system_tool_calls_only_its_fixed_service_method() -> None:
    registry, profiles, remote = _registry()
    registry.execute("get_remote_system_info", {"profile_name": "server"})
    registry.execute("check_remote_memory", {"profile_name": "server"})
    registry.execute("check_remote_disk", {"profile_name": "server"})
    assert profiles.calls == ["server", "server", "server"]
    assert remote.calls == [
        ("system", "server"),
        ("memory", "server"),
        ("disk", "server"),
    ]


def test_system_tool_results_do_not_expose_profile_credentials_or_remote_command() -> None:
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
        "ssh-ed25519",
        "/proc/meminfo",
        "df -Pk",
    ):
        assert forbidden not in serialized


def test_all_system_tool_results_are_json_safe_and_deterministic() -> None:
    first_registry, _, _ = _registry()
    second_registry, _, _ = _registry()
    first = [
        first_registry.execute(item.name, {"profile_name": "server"}).to_dict()
        for item in first_registry.list_definitions()
    ]
    second = [
        second_registry.execute(item.name, {"profile_name": "server"}).to_dict()
        for item in second_registry.list_definitions()
    ]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_malicious_profile_name_never_reaches_remote_system_service() -> None:
    registry, profiles, remote = _registry()
    result = registry.execute(
        "check_remote_memory",
        {"profile_name": "server; cat /etc/shadow"},
    )
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert profiles.calls == []
    assert remote.calls == []


def test_failed_system_report_returns_no_fake_metrics() -> None:
    class Unavailable(FakeRemoteSystem):
        def check_memory(self, profile: Profile) -> RemoteMemoryReport:
            return RemoteMemoryReport(
                CheckResult(
                    "Remote memory",
                    CheckStatus.FAIL,
                    "remote system check was unavailable",
                    "REMOTE_SYSTEM_UNAVAILABLE",
                )
            )

    profiles = FakeProfiles()
    registry = ToolRegistry()
    for tool in build_system_tools(profiles, Unavailable()):
        registry.register(tool)
    result = registry.execute("check_remote_memory", {"profile_name": "server"})
    assert result.data["metrics"] == {}
    assert result.data["evidence"][0]["error_code"] == "REMOTE_SYSTEM_UNAVAILABLE"
