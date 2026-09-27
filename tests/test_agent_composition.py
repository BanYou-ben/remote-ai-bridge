from __future__ import annotations

from pathlib import Path

from app import bootstrap
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.domain.supervisor import SupervisorSnapshot, SupervisorState
from app.services.agent.registry_factory import build_agent_tool_registry
from app.services.ai.context_builder import build_doctor_evidence
from app.services.doctor import DoctorReport
from app.services.local_proxy import LocalProxyReport
from app.services.network_transport import NetworkTransportReport, NetworkTransportService
from app.services.remote_system import RemoteMemoryReport, RemoteSystemService


EXPECTED_TOOL_NAMES = [
    "check_local_proxy",
    "check_remote_disk",
    "check_remote_endpoint",
    "check_remote_listener",
    "check_remote_memory",
    "check_ssh_config",
    "check_ssh_transport",
    "check_tunnel_process",
    "get_profile_summary",
    "get_remote_system_info",
    "get_runtime_status",
    "run_doctor",
]


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


def _check(name: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASS, "safe")


class FakeProfiles:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def get(self, name: str) -> Profile:
        self.calls.append(f"profiles:{name}")
        return _profile()


class FakeRuntime:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def status(self, name: str) -> SupervisorSnapshot:
        self.calls.append(f"runtime:{name}")
        return SupervisorSnapshot(
            profile_name=name,
            state=SupervisorState.STOPPED,
            updated_at="2026-01-01T00:00:00+00:00",
            message="stopped",
        )


class FakeDoctor:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def run(self, profile: Profile) -> DoctorReport:
        self.calls.append(f"doctor:{profile.name}")
        return DoctorReport(
            LocalProxyReport(_check("TCP"), _check("Handshake"), _check("Endpoint")),
            _check("SSH config"),
            _check("Tunnel"),
            _check("Listener"),
            _check("Remote endpoint"),
        )


class FakeLocalProxy:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def inspect(self, profile: Profile) -> LocalProxyReport:
        self.calls.append(f"proxy:{profile.name}")
        return LocalProxyReport(_check("TCP"), _check("Handshake"), _check("Endpoint"))


class FakeSSHConfig:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def check(self, profile: Profile) -> CheckResult:
        self.calls.append(f"ssh:{profile.name}")
        return _check("SSH config")


class FakeTunnel:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def process_check(self, name: str) -> CheckResult:
        self.calls.append(f"tunnel:{name}")
        return _check("Tunnel")


class FakeRemoteProbe:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def check_listener(self, profile: Profile) -> CheckResult:
        self.calls.append(f"listener:{profile.name}")
        return _check("Listener")

    def check_endpoint(self, profile: Profile) -> CheckResult:
        self.calls.append(f"endpoint:{profile.name}")
        return _check("Remote endpoint")


class FakeNetworkTransport:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def check_ssh_transport(self, profile: Profile) -> NetworkTransportReport:
        self.calls.append(f"transport:{profile.name}")
        return NetworkTransportReport(_check("DNS"), _check("TCP"))


class FakeRemoteSystem:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def get_system_info(self, profile: Profile):
        self.calls.append(f"system:{profile.name}")
        raise AssertionError("not used by composition smoke")

    def check_memory(self, profile: Profile) -> RemoteMemoryReport:
        self.calls.append(f"memory:{profile.name}")
        return RemoteMemoryReport(
            CheckResult("Remote memory", CheckStatus.PASS, "safe"),
            1000,
            500,
            50.0,
        )

    def check_disk(self, profile: Profile):
        self.calls.append(f"disk:{profile.name}")
        raise AssertionError("not used by composition smoke")


def _dependencies(calls: list[str]) -> dict[str, object]:
    return {
        "profiles": FakeProfiles(calls),
        "runtime_manager": FakeRuntime(calls),
        "doctor": FakeDoctor(calls),
        "local_proxy": FakeLocalProxy(calls),
        "ssh_config": FakeSSHConfig(calls),
        "tunnel_manager": FakeTunnel(calls),
        "remote_probe": FakeRemoteProbe(calls),
        "network_transport": FakeNetworkTransport(calls),
        "remote_system": FakeRemoteSystem(calls),
    }


def test_create_services_exposes_agent_diagnostic_dependencies(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.bootstrap.locate_ssh", lambda: "ssh")
    services = bootstrap.create_services(tmp_path)
    assert isinstance(services.network_transport, NetworkTransportService)
    assert isinstance(services.remote_system, RemoteSystemService)
    assert services.remote_system.runner is services.remote_probe.runner
    assert services.remote_system.remote_probe is services.remote_probe
    assert services.agent_tool_registry is not None


def test_create_services_passes_its_exact_service_instances_to_registry_factory(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.bootstrap.locate_ssh", lambda: "ssh")
    original = bootstrap.build_agent_tool_registry
    captured: dict[str, object] = {}

    def capture(**dependencies):
        captured.update(dependencies)
        return original(**dependencies)

    monkeypatch.setattr(bootstrap, "build_agent_tool_registry", capture)
    services = bootstrap.create_services(tmp_path)
    assert captured == {
        "profiles": services.profiles,
        "runtime_manager": services.runtime_manager,
        "doctor": services.doctor,
        "local_proxy": services.local_proxy,
        "ssh_config": services.ssh_config,
        "tunnel_manager": services.tunnel_manager,
        "remote_probe": services.remote_probe,
        "network_transport": services.network_transport,
        "remote_system": services.remote_system,
    }


def test_registry_factory_registers_exact_complete_tool_set() -> None:
    registry = build_agent_tool_registry(**_dependencies([]))
    assert [item.name for item in registry.list_definitions()] == EXPECTED_TOOL_NAMES
    assert len(registry.list_definitions()) == 12


def test_registry_factory_registers_only_read_only_tools() -> None:
    registry = build_agent_tool_registry(**_dependencies([]))
    assert {item.risk_level for item in registry.list_definitions()} == {"read_only"}


def test_registry_category_distribution_is_stable() -> None:
    registry = build_agent_tool_registry(**_dependencies([]))
    categories = [item.category for item in registry.list_definitions()]
    assert categories.count("connection") == 8
    assert categories.count("network") == 1
    assert categories.count("system") == 3


def test_registry_has_no_action_or_shell_tools() -> None:
    registry = build_agent_tool_registry(**_dependencies([]))
    names = {item.name for item in registry.list_definitions()}
    assert names.isdisjoint(
        {
            "connect",
            "disconnect",
            "delete_profile",
            "update_profile",
            "kill_process",
            "run_shell",
        }
    )


def test_registry_definition_order_is_repeatable() -> None:
    registry = build_agent_tool_registry(**_dependencies([]))
    assert registry.list_definitions() == registry.list_definitions()
    first = [item.to_dict() for item in registry.list_definitions()]
    second = [
        item.to_dict()
        for item in build_agent_tool_registry(**_dependencies([])).list_definitions()
    ]
    assert first == second


def test_registry_factory_has_no_execution_side_effects() -> None:
    calls: list[str] = []
    registry = build_agent_tool_registry(**_dependencies(calls))
    assert len(registry.list_definitions()) == 12
    assert calls == []


def test_factory_composition_smoke_executes_connection_and_system_tools() -> None:
    calls: list[str] = []
    registry = build_agent_tool_registry(**_dependencies(calls))
    profile = registry.execute("get_profile_summary", {"profile_name": "server"})
    memory = registry.execute("check_remote_memory", {"profile_name": "server"})
    assert profile.ok is True
    assert profile.data["name"] == "server"
    assert memory.ok is True
    assert memory.data["evidence"][0]["id"] == "system.remote.memory"
    assert calls == ["profiles:server", "profiles:server", "memory:server"]


def test_bootstrap_does_not_construct_an_agent_model() -> None:
    source = Path(bootstrap.__file__).read_text(encoding="utf-8")
    assert "OpenAICompatibleResponsesAgentModel" not in source
    assert "AgentRunner(" not in source


def test_doctor_fixture_uses_canonical_evidence_without_execution_during_factory() -> None:
    calls: list[str] = []
    dependencies = _dependencies(calls)
    registry = build_agent_tool_registry(**dependencies)
    assert calls == []
    report = dependencies["doctor"].run(_profile())
    assert len(build_doctor_evidence(report)) == 7
    assert len(registry.list_definitions()) == 12
