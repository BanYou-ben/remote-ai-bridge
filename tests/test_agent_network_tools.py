from __future__ import annotations

import json

import pytest

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.services.agent.network_tools import build_network_tools
from app.services.agent.tool_registry import ToolRegistry
from app.services.ai.context_builder import build_doctor_evidence
from app.services.doctor import DoctorReport
from app.services.local_proxy import LocalProxyReport
from app.services.network_transport import NetworkTransportReport


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


def _check(name: str, status: CheckStatus = CheckStatus.PASS, detail: str = "safe") -> CheckResult:
    return CheckResult(name, status, detail)


class FakeProfiles:
    def __init__(self) -> None:
        self.profile = _profile()
        self.names: list[str] = []

    def get(self, name: str) -> Profile:
        self.names.append(name)
        return self.profile


class FakeLocalProxy:
    def __init__(self) -> None:
        self.profiles: list[Profile] = []
        self.report = LocalProxyReport(_check("TCP"), _check("Handshake"), _check("Endpoint"))

    def inspect(self, profile: Profile) -> LocalProxyReport:
        self.profiles.append(profile)
        return self.report


class FakeSSHConfig:
    def __init__(self) -> None:
        self.profiles: list[Profile] = []

    def check(self, profile: Profile) -> CheckResult:
        self.profiles.append(profile)
        return _check("SSH config")


class FakeTransport:
    def __init__(self, report: NetworkTransportReport | None = None) -> None:
        self.profiles: list[Profile] = []
        self.report = report or NetworkTransportReport(_check("DNS"), _check("TCP"))

    def check_ssh_transport(self, profile: Profile) -> NetworkTransportReport:
        self.profiles.append(profile)
        return self.report


class FakeTunnel:
    def __init__(self) -> None:
        self.names: list[str] = []

    def process_check(self, name: str) -> CheckResult:
        self.names.append(name)
        return _check("Tunnel")


class FakeRemoteProbe:
    def __init__(self) -> None:
        self.listener_profiles: list[Profile] = []
        self.endpoint_profiles: list[Profile] = []

    def check_listener(self, profile: Profile) -> CheckResult:
        self.listener_profiles.append(profile)
        return _check("Listener")

    def check_endpoint(self, profile: Profile) -> CheckResult:
        self.endpoint_profiles.append(profile)
        return _check("Remote endpoint")


def _services(report: NetworkTransportReport | None = None):
    return (
        FakeProfiles(),
        FakeLocalProxy(),
        FakeSSHConfig(),
        FakeTransport(report),
        FakeTunnel(),
        FakeRemoteProbe(),
    )


def _registry(report: NetworkTransportReport | None = None):
    services = _services(report)
    registry = ToolRegistry()
    for tool in build_network_tools(*services):
        registry.register(tool)
    return registry, services


def test_granular_tool_names_are_stable() -> None:
    registry, _ = _registry()
    assert [item.name for item in registry.list_definitions()] == [
        "check_local_proxy",
        "check_remote_endpoint",
        "check_remote_listener",
        "check_ssh_config",
        "check_ssh_transport",
        "check_tunnel_process",
    ]


def test_all_granular_tools_are_read_only() -> None:
    registry, _ = _registry()
    assert {item.risk_level for item in registry.list_definitions()} == {"read_only"}


def test_transport_tool_uses_network_category_only() -> None:
    registry, _ = _registry()
    categories = {item.name: item.category for item in registry.list_definitions()}
    assert categories["check_ssh_transport"] == "network"
    assert {value for key, value in categories.items() if key != "check_ssh_transport"} == {
        "connection"
    }


def test_all_granular_tools_only_accept_profile_name() -> None:
    registry, _ = _registry()
    for definition in registry.list_definitions():
        schema = definition.input_schema
        assert set(schema["properties"]) == {"profile_name"}
        assert schema["required"] == ["profile_name"]
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "tool_name",
    [
        "check_local_proxy",
        "check_ssh_config",
        "check_ssh_transport",
        "check_tunnel_process",
        "check_remote_listener",
        "check_remote_endpoint",
    ],
)
def test_extra_network_tool_arguments_are_rejected(tool_name: str) -> None:
    registry, services = _registry()
    result = registry.execute(
        tool_name,
        {"profile_name": "server", "host": "scan.example", "port": 3389},
    )
    assert result.error_code == "TOOL_ARGUMENT_INVALID"
    assert services[0].names == []


@pytest.mark.parametrize(
    ("tool_name", "expected_ids"),
    [
        (
            "check_local_proxy",
            [
                "connection.proxy.tcp",
                "connection.proxy.handshake",
                "connection.proxy.endpoint",
            ],
        ),
        ("check_ssh_config", ["connection.ssh.config"]),
        ("check_ssh_transport", ["network.ssh.dns", "network.ssh.tcp"]),
        ("check_tunnel_process", ["connection.tunnel.process"]),
        ("check_remote_listener", ["connection.remote.listener"]),
        ("check_remote_endpoint", ["connection.remote.endpoint"]),
    ],
)
def test_granular_tool_evidence_ids(tool_name: str, expected_ids: list[str]) -> None:
    registry, _ = _registry()
    result = registry.execute(tool_name, {"profile_name": "server"})
    assert result.ok is True
    assert [item["id"] for item in result.data["evidence"]] == expected_ids


def test_each_tool_calls_its_existing_service_with_loaded_profile() -> None:
    registry, services = _registry()
    for name in [item.name for item in registry.list_definitions()]:
        registry.execute(name, {"profile_name": "server"})
    profiles, local, ssh, transport, tunnel, remote = services
    assert profiles.names == ["server"] * 6
    assert local.profiles == [profiles.profile]
    assert ssh.profiles == [profiles.profile]
    assert transport.profiles == [profiles.profile]
    assert tunnel.names == ["server"]
    assert remote.listener_profiles == [profiles.profile]
    assert remote.endpoint_profiles == [profiles.profile]


def test_transport_evidence_category_status_and_error_are_preserved() -> None:
    report = NetworkTransportReport(
        CheckResult("DNS", CheckStatus.PASS, "resolved"),
        CheckResult("TCP", CheckStatus.FAIL, "timed out", "SSH_TCP_TIMEOUT"),
    )
    registry, _ = _registry(report)
    result = registry.execute("check_ssh_transport", {"profile_name": "server"})
    dns, tcp = result.data["evidence"]
    assert dns["category"] == tcp["category"] == "network"
    assert (dns["status"], tcp["status"]) == ("PASS", "FAIL")
    assert tcp["error_code"] == "SSH_TCP_TIMEOUT"


def test_tool_output_does_not_expose_profile_ssh_credentials() -> None:
    registry, _ = _registry()
    outputs = [
        registry.execute(item.name, {"profile_name": "server"}).to_dict()
        for item in registry.list_definitions()
    ]
    serialized = json.dumps(outputs)
    for forbidden in (
        "alice",
        "ssh.example.test",
        "11111111111111111111111111111111",
        "SHA256:",
        "ssh-ed25519",
    ):
        assert forbidden not in serialized


def test_granular_connection_mapping_matches_doctor_mapping() -> None:
    registry, services = _registry()
    _, local, ssh, _, tunnel, remote = services
    doctor = DoctorReport(
        local.report,
        ssh.check(_profile()),
        tunnel.process_check("server"),
        remote.check_listener(_profile()),
        remote.check_endpoint(_profile()),
    )
    expected = {item.id: item.to_dict() for item in build_doctor_evidence(doctor)}
    actual: dict[str, dict[str, object]] = {}
    for name in (
        "check_local_proxy",
        "check_ssh_config",
        "check_tunnel_process",
        "check_remote_listener",
        "check_remote_endpoint",
    ):
        result = registry.execute(name, {"profile_name": "server"})
        actual.update({item["id"]: item for item in result.data["evidence"]})
    assert actual == expected
