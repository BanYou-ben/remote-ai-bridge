from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Protocol, TypeAlias

from app.domain.profile import ProfileValidationError, validate_profile_name
from app.redaction import redact, redact_details
from app.services.agent.tool_contract import ToolDefinition, ToolResult
from app.services.ai.diagnosis import StructuredDiagnosis
from app.services.ai.schemas import DiagnosticEvidence


@dataclass(frozen=True)
class AgentRequest:
    profile_name: str
    message: str

    def __post_init__(self) -> None:
        try:
            validate_profile_name(self.profile_name)
        except ProfileValidationError as exc:
            raise ValueError("invalid agent profile name") from exc
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("agent request message must not be empty")
        object.__setattr__(self, "message", redact(self.message))

    def to_dict(self) -> dict[str, object]:
        return {"profile_name": self.profile_name, "message": self.message}


@dataclass(frozen=True)
class ToolCallDecision:
    tool_name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool decision name must not be empty")
        safe_arguments = _safe_object(self.arguments, "tool decision arguments")
        object.__setattr__(self, "arguments", safe_arguments)


@dataclass(frozen=True)
class FinalDiagnosisDecision:
    diagnosis: StructuredDiagnosis

    def __post_init__(self) -> None:
        if not isinstance(self.diagnosis, StructuredDiagnosis):
            raise ValueError("final decision must contain a structured diagnosis")


AgentDecision: TypeAlias = ToolCallDecision | FinalDiagnosisDecision


@dataclass(frozen=True)
class AgentObservation:
    tool_name: str
    arguments: dict[str, Any]
    ok: bool
    data: dict[str, Any]
    error_code: str | None
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", _safe_object(self.arguments, "observation arguments"))
        object.__setattr__(self, "data", _safe_object(self.data, "observation data"))
        object.__setattr__(self, "message", redact(self.message))

    @classmethod
    def from_tool_result(
        cls,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> AgentObservation:
        return cls(
            tool_name=result.tool_name,
            arguments=arguments,
            ok=result.ok,
            data=result.data,
            error_code=result.error_code,
            message=result.message,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "ok": self.ok,
            "data": self.data,
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class AgentState:
    step_count: int = 0
    observations: tuple[AgentObservation, ...] = ()
    evidence: tuple[DiagnosticEvidence, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "step_count": self.step_count,
            "observations": [item.to_dict() for item in self.observations],
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class AgentRunResult:
    status: str
    diagnosis: StructuredDiagnosis | None
    observations: tuple[AgentObservation, ...]
    step_count: int
    error_code: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed"}:
            raise ValueError("invalid agent run status")
        if self.status == "completed" and self.diagnosis is None:
            raise ValueError("completed agent run requires a diagnosis")
        if self.status == "failed" and self.diagnosis is not None:
            raise ValueError("failed agent run cannot include a diagnosis")
        object.__setattr__(self, "message", redact(self.message))

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "diagnosis": self.diagnosis.to_dict() if self.diagnosis else None,
            "observations": [item.to_dict() for item in self.observations],
            "step_count": self.step_count,
            "error_code": self.error_code,
            "message": self.message,
        }


class AgentModel(Protocol):
    def decide(
        self,
        request: AgentRequest,
        state: AgentState,
        tools: tuple[ToolDefinition, ...],
    ) -> AgentDecision:
        ...


class AgentModelError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.message = redact(message)
        self.retryable = retryable
        super().__init__(self.message)


def _safe_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    safe_value = redact_details(value)
    if not isinstance(safe_value, dict):
        raise ValueError(f"{label} must be an object")
    try:
        json.dumps(safe_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    return safe_value
