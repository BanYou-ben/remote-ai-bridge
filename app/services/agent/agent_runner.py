from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.redaction import redact
from app.services.agent.agent_contract import (
    AgentModel,
    AgentObservation,
    AgentRequest,
    AgentRunResult,
    AgentState,
    FinalDiagnosisDecision,
    ToolCallDecision,
)
from app.services.agent.tool_contract import ToolDefinition, ToolResult
from app.services.agent.tool_registry import ToolNotFoundError, ToolRegistry
from app.services.ai.diagnosis import (
    DiagnosisValidationError,
    StructuredDiagnosis,
    validate_diagnosis_against_evidence,
)
from app.services.ai.schemas import DiagnosticEvidence


class AgentRunner:
    def __init__(
        self,
        registry: ToolRegistry,
        model: AgentModel,
        *,
        max_steps: int = 6,
    ) -> None:
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")
        self._registry = registry
        self._model = model
        self.max_steps = max_steps

    def run(self, request: AgentRequest) -> AgentRunResult:
        state = AgentState()
        for _ in range(self.max_steps):
            tools = tuple(
                _copy_definition(item)
                for item in self._registry.list_definitions()
                if item.risk_level == "read_only"
            )
            try:
                decision = self._model.decide(request, state, tools)
            except Exception:
                return self._failed(state, "AGENT_MODEL_FAILED", "agent model failed")

            if isinstance(decision, FinalDiagnosisDecision):
                try:
                    validate_diagnosis_against_evidence(decision.diagnosis, state.evidence)
                except (DiagnosisValidationError, TypeError, ValueError, AttributeError):
                    return self._failed(
                        state,
                        "AGENT_DIAGNOSIS_INVALID",
                        "agent diagnosis is invalid or not grounded",
                    )
                return AgentRunResult(
                    status="completed",
                    diagnosis=_redact_diagnosis(decision.diagnosis),
                    observations=state.observations,
                    step_count=state.step_count,
                )

            if not isinstance(decision, ToolCallDecision):
                return self._failed(state, "AGENT_DECISION_INVALID", "agent decision is invalid")

            result = self._execute_tool(request, decision)
            observation = AgentObservation.from_tool_result(decision.arguments, result)
            try:
                evidence = self._collect_evidence(state.evidence, observation)
            except ValueError:
                observation = AgentObservation.from_tool_result(
                    decision.arguments,
                    ToolResult(
                        decision.tool_name,
                        False,
                        {},
                        "AGENT_EVIDENCE_INVALID",
                        "tool returned invalid diagnostic evidence",
                    ),
                )
                evidence = state.evidence
            state = AgentState(
                step_count=state.step_count + 1,
                observations=(*state.observations, observation),
                evidence=evidence,
            )

        return self._failed(state, "AGENT_MAX_STEPS", "agent exceeded the maximum number of steps")

    def _execute_tool(self, request: AgentRequest, decision: ToolCallDecision) -> ToolResult:
        try:
            tool = self._registry.get(decision.tool_name)
        except ToolNotFoundError:
            return self._registry.execute(decision.tool_name, decision.arguments)

        if tool.definition.risk_level != "read_only":
            return ToolResult(
                decision.tool_name,
                False,
                {},
                "AGENT_TOOL_NOT_ALLOWED",
                "tool is not allowed by the read-only agent policy",
            )

        if "profile_name" in tool.arguments_model.model_fields:
            supplied_profile = decision.arguments.get("profile_name")
            if supplied_profile is not None and supplied_profile != request.profile_name:
                return ToolResult(
                    decision.tool_name,
                    False,
                    {},
                    "AGENT_PROFILE_SCOPE_VIOLATION",
                    "tool call profile is outside the agent request scope",
                )

        return self._registry.execute(decision.tool_name, decision.arguments)

    @staticmethod
    def _collect_evidence(
        existing: tuple[DiagnosticEvidence, ...],
        observation: AgentObservation,
    ) -> tuple[DiagnosticEvidence, ...]:
        raw_evidence = observation.data.get("evidence")
        if raw_evidence is None:
            return existing
        if not isinstance(raw_evidence, list):
            raise ValueError("evidence must be a list")
        catalog = {item.id: item for item in existing}
        for raw_item in raw_evidence:
            item = _parse_evidence(raw_item)
            catalog[item.id] = item
        return tuple(catalog[evidence_id] for evidence_id in sorted(catalog))

    @staticmethod
    def _failed(state: AgentState, error_code: str, message: str) -> AgentRunResult:
        return AgentRunResult(
            status="failed",
            diagnosis=None,
            observations=state.observations,
            step_count=state.step_count,
            error_code=error_code,
            message=message,
        )


def _parse_evidence(value: object) -> DiagnosticEvidence:
    if not isinstance(value, dict) or set(value) != {
        "id",
        "category",
        "name",
        "status",
        "detail",
        "error_code",
        "http_status",
    }:
        raise ValueError("invalid evidence object")
    for field in ("id", "category", "name", "detail"):
        if not isinstance(value[field], str) or not value[field].strip():
            raise ValueError("invalid evidence string field")
    if value["status"] not in {"PASS", "FAIL", "SKIP"}:
        raise ValueError("invalid evidence status")
    if value["error_code"] is not None and not isinstance(value["error_code"], str):
        raise ValueError("invalid evidence error code")
    http_status = value["http_status"]
    if http_status is not None and (isinstance(http_status, bool) or not isinstance(http_status, int)):
        raise ValueError("invalid evidence HTTP status")
    return DiagnosticEvidence(
        id=value["id"],
        category=value["category"],
        name=value["name"],
        status=value["status"],
        detail=value["detail"],
        error_code=value["error_code"],
        http_status=http_status,
    )


def _redact_diagnosis(diagnosis: StructuredDiagnosis) -> StructuredDiagnosis:
    return StructuredDiagnosis(
        summary=redact(diagnosis.summary),
        diagnosis_stage=diagnosis.diagnosis_stage,
        evidence_ids=diagnosis.evidence_ids,
        possible_causes=tuple(redact(item) for item in diagnosis.possible_causes),
        recommended_actions=tuple(redact(item) for item in diagnosis.recommended_actions),
        confidence=diagnosis.confidence,
    )


def _copy_definition(definition: ToolDefinition) -> ToolDefinition:
    return ToolDefinition(
        name=definition.name,
        description=definition.description,
        category=definition.category,
        risk_level=definition.risk_level,
        input_schema=deepcopy(definition.input_schema),
    )
