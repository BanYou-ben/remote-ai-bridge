from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

import pytest

from app.services.agent.agent_contract import (
    AgentModelError,
    AgentRequest,
    AgentState,
    FinalDiagnosisDecision,
    ToolCallDecision,
)
from app.services.agent.agent_runner import AgentRunner
from app.services.agent.tool_contract import (
    ProfileNameArguments,
    Tool,
    ToolDefinition,
    ToolResult,
    profile_tool_definition,
)
from app.services.agent.tool_registry import ToolRegistry
from app.services.ai.diagnosis import StructuredDiagnosis


EVIDENCE = {
    "id": "connection.tunnel.process",
    "category": "connection",
    "name": "Tunnel process",
    "status": "FAIL",
    "detail": "process exited",
    "error_code": "PROCESS_EXITED",
    "http_status": None,
}


def _diagnosis(
    *,
    evidence_ids: tuple[str, ...] = ("connection.tunnel.process",),
    stage: str = "connection.tunnel",
) -> StructuredDiagnosis:
    return StructuredDiagnosis(
        summary="The tunnel process exited.",
        diagnosis_stage=stage,
        evidence_ids=evidence_ids,
        possible_causes=("SSH transport ended.",),
        recommended_actions=("Run diagnostics again.",),
        confidence="high",
    )


@dataclass
class ScriptedAgentModel:
    decisions: list[object]
    states: list[AgentState] = field(default_factory=list)
    tool_lists: list[tuple[str, ...]] = field(default_factory=list)

    def decide(self, request, state, tools):
        self.states.append(state)
        self.tool_lists.append(tuple(item.name for item in tools))
        decision = self.decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        return decision


def _tool(name: str, calls: list[str], result: ToolResult | None = None, *, risk="read_only") -> Tool:
    def handler(arguments):
        calls.append(f"{name}:{arguments.profile_name}")
        return result or ToolResult(name, True, {"profile": arguments.profile_name})

    definition = profile_tool_definition(name, f"Run {name}.")
    if risk != "read_only":
        definition = ToolDefinition(
            name=definition.name,
            description=definition.description,
            category=definition.category,
            risk_level=risk,
            input_schema=definition.input_schema,
        )
    return Tool(definition, ProfileNameArguments, handler)


def _registry(*, failing_runtime: bool = False) -> tuple[ToolRegistry, list[str]]:
    calls: list[str] = []
    registry = ToolRegistry()
    if failing_runtime:
        def fail(_arguments):
            calls.append("get_runtime_status:server")
            raise RuntimeError("password=RAB-SECRET")

        registry.register(
            Tool(
                profile_tool_definition("get_runtime_status", "Get runtime status."),
                ProfileNameArguments,
                fail,
            )
        )
    else:
        registry.register(_tool("get_runtime_status", calls))
    registry.register(
        _tool("run_doctor", calls, ToolResult("run_doctor", True, {"evidence": [EVIDENCE]}))
    )
    return registry, calls


def test_multi_step_agent_run_completes_with_grounded_diagnosis() -> None:
    registry, calls = _registry()
    model = ScriptedAgentModel(
        [
            ToolCallDecision("get_runtime_status", {"profile_name": "server"}),
            ToolCallDecision("run_doctor", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Why is it offline?"))
    assert result.status == "completed"
    assert result.diagnosis == _diagnosis()
    assert calls == ["get_runtime_status:server", "run_doctor:server"]
    assert result.step_count == 2


def test_model_sees_prior_observations_on_each_decision() -> None:
    registry, _ = _registry()
    model = ScriptedAgentModel(
        [
            ToolCallDecision("get_runtime_status", {"profile_name": "server"}),
            ToolCallDecision("run_doctor", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    AgentRunner(registry, model).run(AgentRequest("server", "Diagnose"))
    assert [len(state.observations) for state in model.states] == [0, 1, 2]
    assert [state.step_count for state in model.states] == [0, 1, 2]


def test_tool_definitions_are_passed_deterministically_to_model() -> None:
    registry, _ = _registry()
    model = ScriptedAgentModel([ToolCallDecision("missing", {}), ToolCallDecision("missing", {})])
    AgentRunner(registry, model, max_steps=2).run(AgentRequest("server", "Diagnose"))
    assert model.tool_lists == [
        ("get_runtime_status", "run_doctor"),
        ("get_runtime_status", "run_doctor"),
    ]


def test_final_diagnosis_cannot_reference_unobserved_evidence() -> None:
    registry, _ = _registry()
    model = ScriptedAgentModel(
        [
            ToolCallDecision("get_runtime_status", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose"))
    assert (result.status, result.error_code) == ("failed", "AGENT_DIAGNOSIS_INVALID")


def test_cross_category_grounding_is_rejected() -> None:
    registry, _ = _registry()
    model = ScriptedAgentModel(
        [
            ToolCallDecision("run_doctor", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis(stage="system.memory")),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose"))
    assert result.error_code == "AGENT_DIAGNOSIS_INVALID"


@pytest.mark.parametrize(
    ("first_decision", "expected_error"),
    [
        (ToolCallDecision("missing", {"profile_name": "server"}), "TOOL_NOT_FOUND"),
        (ToolCallDecision("get_runtime_status", {}), "TOOL_ARGUMENT_INVALID"),
    ],
)
def test_tool_failures_become_observations_and_agent_continues(first_decision, expected_error) -> None:
    registry, _ = _registry()
    model = ScriptedAgentModel(
        [
            first_decision,
            ToolCallDecision("run_doctor", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose"))
    assert result.status == "completed"
    assert result.observations[0].error_code == expected_error
    assert len(result.observations) == 2


def test_tool_execution_exception_is_safe_and_agent_continues() -> None:
    registry, _ = _registry(failing_runtime=True)
    model = ScriptedAgentModel(
        [
            ToolCallDecision("get_runtime_status", {"profile_name": "server"}),
            ToolCallDecision("run_doctor", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose"))
    assert result.status == "completed"
    assert result.observations[0].error_code == "TOOL_EXECUTION_FAILED"
    assert "RAB-SECRET" not in json.dumps(result.to_dict())


def test_profile_scope_violation_never_executes_tool() -> None:
    registry, calls = _registry()
    model = ScriptedAgentModel([ToolCallDecision("run_doctor", {"profile_name": "other"})])
    result = AgentRunner(registry, model, max_steps=1).run(AgentRequest("server", "Diagnose"))
    assert calls == []
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"


def test_profile_scope_uses_trusted_argument_model_not_exported_schema() -> None:
    registry, calls = _registry()

    class SchemaMutatingModel:
        def decide(self, request, state, tools):
            tools[1].input_schema["properties"].pop("profile_name", None)
            return ToolCallDecision("run_doctor", {"profile_name": "other"})

    result = AgentRunner(registry, SchemaMutatingModel(), max_steps=1).run(
        AgentRequest("server", "Diagnose")
    )
    assert calls == []
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"
    registered = registry.get("run_doctor")
    assert "profile_name" in registered.definition.input_schema["properties"]


def test_non_read_only_tool_is_rejected_without_execution() -> None:
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(_tool("disconnect", calls, risk="action"))
    model = ScriptedAgentModel([ToolCallDecision("disconnect", {"profile_name": "server"})])
    result = AgentRunner(registry, model, max_steps=1).run(AgentRequest("server", "Diagnose"))
    assert calls == []
    assert result.observations[0].error_code == "AGENT_TOOL_NOT_ALLOWED"


def test_model_is_only_exposed_to_read_only_tool_definitions() -> None:
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(_tool("read_probe", calls))
    registry.register(_tool("disconnect", calls, risk="action"))
    model = ScriptedAgentModel([ToolCallDecision("read_probe", {"profile_name": "server"})])
    AgentRunner(registry, model, max_steps=1).run(AgentRequest("server", "Diagnose"))
    assert model.tool_lists == [("read_probe",)]


def test_each_decision_receives_a_fresh_unmodified_tool_schema() -> None:
    registry, _ = _registry()

    class MutatingModel:
        calls = 0

        def decide(self, request, state, tools):
            self.calls += 1
            properties = tools[0].input_schema["properties"]
            if self.calls == 1:
                properties.pop("profile_name")
                return ToolCallDecision("missing", {})
            assert "profile_name" in properties
            return ToolCallDecision("missing", {})

    AgentRunner(registry, MutatingModel(), max_steps=2).run(AgentRequest("server", "Diagnose"))
    registered = registry.get("get_runtime_status")
    assert "profile_name" in registered.definition.input_schema["properties"]


def test_malicious_profile_argument_never_reaches_handler() -> None:
    registry, calls = _registry()
    model = ScriptedAgentModel(
        [ToolCallDecision("run_doctor", {"profile_name": "server; shutdown /s"})]
    )
    result = AgentRunner(registry, model, max_steps=1).run(AgentRequest("server", "Diagnose"))
    assert calls == []
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"


def test_tool_result_secret_is_redacted_in_observation() -> None:
    calls: list[str] = []
    registry = ToolRegistry()
    registry.register(
        _tool(
            "get_runtime_status",
            calls,
            ToolResult("get_runtime_status", True, {"message": "password=RAB-SECRET"}),
        )
    )
    model = ScriptedAgentModel([ToolCallDecision("get_runtime_status", {"profile_name": "server"})])
    result = AgentRunner(registry, model, max_steps=1).run(AgentRequest("server", "Diagnose"))
    assert "RAB-SECRET" not in json.dumps(result.to_dict())


def test_final_diagnosis_text_is_redacted_in_run_result() -> None:
    registry, _ = _registry()
    unsafe = StructuredDiagnosis(
        summary="password=RAB-SECRET",
        diagnosis_stage="connection.tunnel",
        evidence_ids=("connection.tunnel.process",),
        possible_causes=("token=RAB-TOKEN",),
        recommended_actions=("Authorization: Bearer RAB-BEARER",),
        confidence="low",
    )
    model = ScriptedAgentModel(
        [
            ToolCallDecision("run_doctor", {"profile_name": "server"}),
            FinalDiagnosisDecision(unsafe),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose"))
    serialized = json.dumps(result.to_dict())
    assert "RAB-SECRET" not in serialized
    assert "RAB-TOKEN" not in serialized
    assert "RAB-BEARER" not in serialized


def test_invalid_evidence_is_rejected_and_not_catalogued() -> None:
    calls: list[str] = []
    registry = ToolRegistry()
    invalid = {**EVIDENCE, "status": "MAYBE"}
    registry.register(_tool("run_doctor", calls, ToolResult("run_doctor", True, {"evidence": [invalid]})))
    model = ScriptedAgentModel([ToolCallDecision("run_doctor", {"profile_name": "server"})])
    result = AgentRunner(registry, model, max_steps=1).run(AgentRequest("server", "Diagnose"))
    assert result.observations[0].error_code == "AGENT_EVIDENCE_INVALID"
    assert result.observations[0].data == {}


def test_evidence_collection_is_not_bound_to_doctor_tool_name() -> None:
    calls: list[str] = []
    network_evidence = {
        **EVIDENCE,
        "id": "network.dns",
        "category": "network",
        "name": "DNS resolution",
    }
    registry = ToolRegistry()
    registry.register(
        _tool(
            "network_probe",
            calls,
            ToolResult("network_probe", True, {"evidence": [network_evidence]}),
        )
    )
    model = ScriptedAgentModel(
        [
            ToolCallDecision("network_probe", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis(evidence_ids=("network.dns",), stage="network.dns")),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose"))
    assert result.status == "completed"
    assert result.diagnosis.evidence_ids == ("network.dns",)


@pytest.mark.parametrize("max_steps", [1, 2, 4])
def test_max_steps_is_configurable_and_bounded(max_steps: int) -> None:
    registry, _ = _registry()
    model = ScriptedAgentModel(
        [ToolCallDecision("missing", {"profile_name": "server"}) for _ in range(max_steps)]
    )
    result = AgentRunner(registry, model, max_steps=max_steps).run(AgentRequest("server", "Diagnose"))
    assert result.error_code == "AGENT_MAX_STEPS"
    assert result.step_count == max_steps
    assert len(result.observations) == max_steps


@pytest.mark.parametrize("max_steps", [0, -1, True, 1.5])
def test_invalid_max_steps_is_rejected(max_steps: object) -> None:
    registry, _ = _registry()
    with pytest.raises(ValueError, match="positive integer"):
        AgentRunner(registry, ScriptedAgentModel([]), max_steps=max_steps)


def test_invalid_agent_decision_returns_structured_failure() -> None:
    registry, _ = _registry()
    result = AgentRunner(registry, ScriptedAgentModel([{"command": "shell"}])).run(
        AgentRequest("server", "Diagnose")
    )
    assert (result.status, result.error_code) == ("failed", "AGENT_DECISION_INVALID")


def test_invalid_final_diagnosis_schema_returns_structured_failure() -> None:
    invalid = object.__new__(StructuredDiagnosis)
    object.__setattr__(invalid, "summary", "")
    object.__setattr__(invalid, "diagnosis_stage", "connection.tunnel")
    object.__setattr__(invalid, "evidence_ids", ("connection.tunnel.process",))
    object.__setattr__(invalid, "possible_causes", ())
    object.__setattr__(invalid, "recommended_actions", ())
    object.__setattr__(invalid, "confidence", "high")
    decision = object.__new__(FinalDiagnosisDecision)
    object.__setattr__(decision, "diagnosis", invalid)
    registry, _ = _registry()
    result = AgentRunner(registry, ScriptedAgentModel([decision])).run(
        AgentRequest("server", "Diagnose")
    )
    assert result.error_code == "AGENT_DIAGNOSIS_INVALID"


def test_agent_model_exception_is_safe_and_redacted() -> None:
    registry, _ = _registry()
    result = AgentRunner(
        registry,
        ScriptedAgentModel([RuntimeError("password=RAB-SECRET")]),
    ).run(AgentRequest("server", "Diagnose"))
    assert result.error_code == "AGENT_MODEL_FAILED"
    assert "RAB-SECRET" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    ("code", "message", "retryable"),
    [
        ("AGENT_MODEL_TIMEOUT", "agent model request timed out", True),
        ("AGENT_MODEL_REFUSAL", "agent model refused the request", False),
    ],
)
def test_agent_model_error_code_is_preserved_by_runner(
    code: str,
    message: str,
    retryable: bool,
) -> None:
    registry, _ = _registry()
    error = AgentModelError(code, message, retryable=retryable)
    result = AgentRunner(registry, ScriptedAgentModel([error])).run(
        AgentRequest("server", "Diagnose")
    )
    assert result.status == "failed"
    assert result.error_code == code
    assert result.message == message


def test_observation_order_is_stable() -> None:
    registry, _ = _registry()
    model = ScriptedAgentModel(
        [
            ToolCallDecision("get_runtime_status", {"profile_name": "server"}),
            ToolCallDecision("run_doctor", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    result = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose"))
    assert [item.tool_name for item in result.observations] == ["get_runtime_status", "run_doctor"]


def test_agent_run_result_is_json_serializable_without_private_reasoning() -> None:
    registry, _ = _registry()
    model = ScriptedAgentModel(
        [
            ToolCallDecision("run_doctor", {"profile_name": "server"}),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    payload = AgentRunner(registry, model).run(AgentRequest("server", "Diagnose")).to_dict()
    serialized = json.dumps(payload)
    assert json.loads(serialized) == payload
    for forbidden in ("thought", "reasoning", "internal_reasoning", "chain_of_thought", "scratchpad"):
        assert forbidden not in serialized


def test_agent_request_validates_profile_and_redacts_message() -> None:
    with pytest.raises(ValueError):
        AgentRequest("bad profile", "Diagnose")
    request = AgentRequest("server", "password=RAB-SECRET")
    assert "RAB-SECRET" not in request.message


def test_agent_runner_has_no_real_model_shell_or_framework_dependency() -> None:
    source = Path("app/services/agent/agent_runner.py").read_text(encoding="utf-8").lower()
    for forbidden in (
        "openai",
        "deepseek",
        "qwen",
        "langchain",
        "langgraph",
        "subprocess",
        "shell=true",
        "fastapi",
    ):
        assert forbidden not in source
