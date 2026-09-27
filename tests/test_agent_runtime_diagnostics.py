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
from app.services.agent.runtime_tools import build_runtime_tools
from app.services.agent.tool_contract import (
    ProfileNameArguments,
    Tool,
    ToolResult,
    profile_scoped_tool_definition,
)
from app.services.agent.tool_registry import ToolRegistry
from app.services.ai.diagnosis import StructuredDiagnosis
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
    def get(self, name: str) -> Profile:
        if name != "server":
            raise AssertionError("unexpected profile lookup")
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
            (RemoteProcessEntry("python", 200.0, 10.0),),
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


@dataclass
class ScriptedModel:
    decisions: list[object]
    states: list[AgentState] = field(default_factory=list)

    def decide(self, request, state, tools):
        self.states.append(state)
        return self.decisions.pop(0)


def _diagnosis(stage: str, evidence_ids: tuple[str, ...]) -> StructuredDiagnosis:
    return StructuredDiagnosis(
        summary="Remote runtime pressure was detected.",
        diagnosis_stage=stage,
        evidence_ids=evidence_ids,
        possible_causes=("A remote runtime resource is unhealthy.",),
        recommended_actions=("Review the reported runtime metrics.",),
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


def _registry() -> tuple[ToolRegistry, FakeRemoteRuntime]:
    registry = ToolRegistry()
    runtime = FakeRemoteRuntime()
    for tool in build_runtime_tools(FakeProfiles(), runtime):
        registry.register(tool)
    registry.register(_network_tool())
    return registry, runtime


def test_agent_completes_multi_step_remote_performance_diagnosis() -> None:
    registry, runtime = _registry()
    model = ScriptedModel(
        [
            ToolCallDecision("check_remote_load", {"profile_name": "server"}),
            ToolCallDecision("get_remote_top_processes", {"profile_name": "server"}),
            FinalDiagnosisDecision(
                _diagnosis(
                    "system.performance",
                    ("system.remote.load", "system.remote.processes"),
                )
            ),
        ]
    )
    result = AgentRunner(registry, model).run(
        AgentRequest("server", "Why is the server slow?")
    )
    assert result.status == "completed"
    assert result.step_count == 2
    assert runtime.calls == ["load", "processes"]
    assert result.diagnosis is not None
    assert result.diagnosis.diagnosis_stage == "system.performance"


def test_agent_completes_failed_service_diagnosis() -> None:
    registry, runtime = _registry()
    model = ScriptedModel(
        [
            ToolCallDecision("check_remote_failed_services", {"profile_name": "server"}),
            FinalDiagnosisDecision(
                _diagnosis("system.services", ("system.remote.services",))
            ),
        ]
    )
    result = AgentRunner(registry, model).run(
        AgentRequest("server", "Are any services failing?")
    )
    assert result.status == "completed"
    assert runtime.calls == ["services"]


def test_system_performance_diagnosis_rejects_network_only_grounding() -> None:
    registry, _ = _registry()
    model = ScriptedModel(
        [
            ToolCallDecision("check_ssh_transport", {"profile_name": "server"}),
            FinalDiagnosisDecision(
                _diagnosis("system.performance", ("network.ssh.tcp",))
            ),
        ]
    )
    result = AgentRunner(registry, model).run(
        AgentRequest("server", "Diagnose performance")
    )
    assert result.status == "failed"
    assert result.error_code == "AGENT_DIAGNOSIS_INVALID"


def test_runtime_tool_profile_scope_violation_never_calls_remote_service() -> None:
    registry, runtime = _registry()
    model = ScriptedModel(
        [ToolCallDecision("check_remote_load", {"profile_name": "other"})]
    )
    result = AgentRunner(registry, model, max_steps=1).run(
        AgentRequest("server", "Check another profile")
    )
    assert result.status == "failed"
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"
    assert runtime.calls == []
