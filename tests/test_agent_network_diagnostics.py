from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.services.agent.agent_contract import (
    AgentRequest,
    AgentState,
    FinalDiagnosisDecision,
    ToolCallDecision,
)
from app.services.agent.agent_runner import AgentRunner
from app.services.agent.network_tools import build_network_tools
from app.services.agent.tool_contract import (
    ProfileNameArguments,
    Tool,
    ToolResult,
    profile_tool_definition,
)
from app.services.agent.tool_registry import ToolRegistry
from app.services.ai.diagnosis import StructuredDiagnosis
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


def _check(
    name: str,
    status: CheckStatus = CheckStatus.PASS,
    detail: str = "safe",
    error_code: str | None = None,
) -> CheckResult:
    return CheckResult(name, status, detail, error_code)


class FakeProfiles:
    def get(self, name: str) -> Profile:
        if name != "server":
            raise AssertionError("unexpected profile lookup")
        return _profile()


class FakeLocalProxy:
    def inspect(self, profile: Profile) -> LocalProxyReport:
        return LocalProxyReport(_check("TCP"), _check("Handshake"), _check("Endpoint"))


class FakeSSHConfig:
    def check(self, profile: Profile) -> CheckResult:
        return _check("SSH config")


class FakeTunnel:
    def process_check(self, name: str) -> CheckResult:
        return _check("Tunnel")


class FakeRemoteProbe:
    def check_listener(self, profile: Profile) -> CheckResult:
        return _check("Listener")

    def check_endpoint(self, profile: Profile) -> CheckResult:
        return _check("Remote endpoint")


class FakeTransport:
    def __init__(self, report: NetworkTransportReport) -> None:
        self.report = report
        self.calls: list[str] = []

    def check_ssh_transport(self, profile: Profile) -> NetworkTransportReport:
        self.calls.append(profile.name)
        return self.report


@dataclass
class ScriptedModel:
    decisions: list[object]
    states: list[AgentState] = field(default_factory=list)

    def decide(self, request, state, tools):
        self.states.append(state)
        return self.decisions.pop(0)


def _diagnosis(*, evidence_ids: tuple[str, ...]) -> StructuredDiagnosis:
    return StructuredDiagnosis(
        summary="The SSH network path is unavailable.",
        diagnosis_stage="network.ssh",
        evidence_ids=evidence_ids,
        possible_causes=("The SSH endpoint did not respond.",),
        recommended_actions=("Check the SSH network path.",),
        confidence="high",
    )


def _registry(report: NetworkTransportReport) -> tuple[ToolRegistry, FakeTransport, list[str]]:
    registry = ToolRegistry()
    calls: list[str] = []

    def profile_summary(arguments: ProfileNameArguments) -> ToolResult:
        calls.append("get_profile_summary")
        return ToolResult("get_profile_summary", True, {"profile": arguments.profile_name})

    registry.register(
        Tool(
            profile_tool_definition("get_profile_summary", "Get the selected profile summary."),
            ProfileNameArguments,
            profile_summary,
        )
    )
    transport = FakeTransport(report)
    for tool in build_network_tools(
        FakeProfiles(),
        FakeLocalProxy(),
        FakeSSHConfig(),
        transport,
        FakeTunnel(),
        FakeRemoteProbe(),
    ):
        registry.register(tool)
    return registry, transport, calls


def test_agent_diagnoses_ssh_tcp_timeout_after_multi_step_network_checks() -> None:
    report = NetworkTransportReport(
        _check("SSH DNS resolution"),
        _check(
            "SSH TCP transport",
            CheckStatus.FAIL,
            "SSH TCP connection timed out",
            "SSH_TCP_TIMEOUT",
        ),
    )
    registry, transport, calls = _registry(report)
    model = ScriptedModel(
        [
            ToolCallDecision("get_profile_summary", {"profile_name": "server"}),
            ToolCallDecision("check_ssh_transport", {"profile_name": "server"}),
            FinalDiagnosisDecision(
                _diagnosis(evidence_ids=("network.ssh.dns", "network.ssh.tcp"))
            ),
        ]
    )

    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose SSH access"))

    assert result.status == "completed"
    assert result.step_count == 2
    assert calls == ["get_profile_summary"]
    assert transport.calls == ["server"]
    assert [item.tool_name for item in result.observations] == [
        "get_profile_summary",
        "check_ssh_transport",
    ]
    assert [item.error_code for item in model.states[-1].evidence] == [
        None,
        "SSH_TCP_TIMEOUT",
    ]


def test_agent_accepts_grounded_dns_failure_with_skipped_tcp_check() -> None:
    report = NetworkTransportReport(
        _check(
            "SSH DNS resolution",
            CheckStatus.FAIL,
            "SSH host name resolution failed",
            "SSH_DNS_RESOLUTION_FAILED",
        ),
        _check(
            "SSH TCP transport",
            CheckStatus.SKIP,
            "SSH TCP check skipped because DNS resolution failed",
        ),
    )
    registry, _, _ = _registry(report)
    model = ScriptedModel(
        [
            ToolCallDecision("check_ssh_transport", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis(evidence_ids=("network.ssh.dns",))),
        ]
    )

    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose DNS"))

    assert result.status == "completed"
    assert result.diagnosis is not None
    assert result.diagnosis.diagnosis_stage == "network.ssh"
    assert model.states[-1].evidence[0].error_code == "SSH_DNS_RESOLUTION_FAILED"


def test_network_tool_call_cannot_escape_agent_profile_scope() -> None:
    registry, transport, _ = _registry(
        NetworkTransportReport(_check("DNS"), _check("TCP"))
    )
    model = ScriptedModel(
        [ToolCallDecision("check_ssh_transport", {"profile_name": "other"})]
    )

    result = AgentRunner(registry, model, max_steps=1).run(
        AgentRequest("server", "Check another target")
    )

    assert result.status == "failed"
    assert result.error_code == "AGENT_MAX_STEPS"
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"
    assert transport.calls == []


def test_network_diagnostic_agent_still_enforces_max_steps() -> None:
    registry, transport, _ = _registry(
        NetworkTransportReport(_check("DNS"), _check("TCP"))
    )
    model = ScriptedModel(
        [ToolCallDecision("check_ssh_transport", {"profile_name": "server"})]
    )

    result = AgentRunner(registry, model, max_steps=1).run(
        AgentRequest("server", "Keep checking")
    )

    assert result.status == "failed"
    assert result.error_code == "AGENT_MAX_STEPS"
    assert result.step_count == 1
    assert transport.calls == ["server"]
