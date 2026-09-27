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
from app.services.agent.system_tools import build_system_tools
from app.services.agent.tool_contract import (
    ProfileNameArguments,
    Tool,
    ToolResult,
    profile_scoped_tool_definition,
)
from app.services.agent.tool_registry import ToolRegistry
from app.services.ai.diagnosis import StructuredDiagnosis
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
    def get(self, name: str) -> Profile:
        if name != "server":
            raise AssertionError("unexpected profile lookup")
        return _profile()


class FakeRemoteSystem:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_system_info(self, profile: Profile) -> RemoteSystemInfo:
        self.calls.append("system")
        return RemoteSystemInfo(
            CheckResult("Remote system information", CheckStatus.PASS, "safe"),
            "Ubuntu",
            "24.04",
            "6.8.0",
            "x86_64",
        )

    def check_memory(self, profile: Profile) -> RemoteMemoryReport:
        self.calls.append("memory")
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
        self.calls.append("disk")
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


@dataclass
class ScriptedModel:
    decisions: list[object]
    states: list[AgentState] = field(default_factory=list)

    def decide(self, request, state, tools):
        self.states.append(state)
        return self.decisions.pop(0)


def _diagnosis(stage: str, evidence_ids: tuple[str, ...]) -> StructuredDiagnosis:
    return StructuredDiagnosis(
        summary="Remote system resource pressure was detected.",
        diagnosis_stage=stage,
        evidence_ids=evidence_ids,
        possible_causes=("A remote resource is exhausted.",),
        recommended_actions=("Review the reported resource metrics.",),
        confidence="high",
    )


def _network_tool() -> Tool:
    def handler(arguments: ProfileNameArguments) -> ToolResult:
        return ToolResult(
            "check_ssh_transport",
            True,
            {
                "evidence": [
                    {
                        "id": "network.ssh.tcp",
                        "category": "network",
                        "name": "SSH TCP transport",
                        "status": "PASS",
                        "detail": "SSH TCP endpoint accepted a connection",
                        "error_code": None,
                        "http_status": None,
                    }
                ]
            },
        )

    return Tool(
        profile_scoped_tool_definition(
            "check_ssh_transport",
            "Check SSH transport.",
            category="network",
        ),
        ProfileNameArguments,
        handler,
    )


def _registry() -> tuple[ToolRegistry, FakeRemoteSystem]:
    registry = ToolRegistry()
    remote = FakeRemoteSystem()
    for tool in build_system_tools(FakeProfiles(), remote):
        registry.register(tool)
    registry.register(_network_tool())
    return registry, remote


def test_agent_completes_multi_step_memory_pressure_diagnosis() -> None:
    registry, remote = _registry()
    model = ScriptedModel(
        [
            ToolCallDecision("get_remote_system_info", {"profile_name": "server"}),
            ToolCallDecision("check_remote_memory", {"profile_name": "server"}),
            FinalDiagnosisDecision(
                _diagnosis("system.memory", ("system.remote.memory",))
            ),
        ]
    )
    result = AgentRunner(registry, model).run(
        AgentRequest("server", "Why is the server slow?")
    )
    assert result.status == "completed"
    assert result.step_count == 2
    assert remote.calls == ["system", "memory"]
    assert result.diagnosis is not None
    assert result.diagnosis.diagnosis_stage == "system.memory"


def test_agent_completes_disk_pressure_diagnosis() -> None:
    registry, remote = _registry()
    model = ScriptedModel(
        [
            ToolCallDecision("check_remote_disk", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis("system.disk", ("system.remote.disk",))),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Check disk usage"))
    assert result.status == "completed"
    assert remote.calls == ["disk"]


def test_cross_domain_agent_grounds_system_diagnosis_in_system_evidence() -> None:
    registry, _ = _registry()
    model = ScriptedModel(
        [
            ToolCallDecision("check_ssh_transport", {"profile_name": "server"}),
            ToolCallDecision("check_remote_memory", {"profile_name": "server"}),
            FinalDiagnosisDecision(
                _diagnosis("system.memory", ("system.remote.memory",))
            ),
        ]
    )
    result = AgentRunner(registry, model).run(
        AgentRequest("server", "Diagnose connectivity and memory")
    )
    assert result.status == "completed"
    assert {item.category for item in model.states[-1].evidence} == {"network", "system"}


def test_cross_domain_agent_rejects_system_diagnosis_with_only_network_evidence() -> None:
    registry, _ = _registry()
    model = ScriptedModel(
        [
            ToolCallDecision("check_ssh_transport", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis("system.memory", ("network.ssh.tcp",))),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose memory"))
    assert result.status == "failed"
    assert result.error_code == "AGENT_DIAGNOSIS_INVALID"


def test_system_tool_profile_scope_violation_never_calls_remote_service() -> None:
    registry, remote = _registry()
    model = ScriptedModel(
        [ToolCallDecision("check_remote_memory", {"profile_name": "other"})]
    )
    result = AgentRunner(registry, model, max_steps=1).run(
        AgentRequest("server", "Check another profile")
    )
    assert result.status == "failed"
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"
    assert remote.calls == []
