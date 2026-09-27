from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretBytes, SecretStr, StrictStr, field_validator

from app.domain.profile import ProfileValidationError, validate_profile_name
from app.redaction import redact, redact_details


class ProfileNameArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile_name: StrictStr

    @field_validator("profile_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        try:
            validate_profile_name(value)
        except ProfileValidationError as exc:
            raise ValueError("invalid profile name") from exc
        return value


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    category: str
    risk_level: str
    input_schema: dict[str, Any]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "risk_level": self.risk_level,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class ToolResult:
    tool_name: str
    ok: bool
    data: dict[str, Any]
    error_code: str | None = None
    message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", redact(self.message))
        redacted_data = _redact_secret_values(self.data)
        if not isinstance(redacted_data, dict):
            raise ValueError("tool result data must be an object")
        try:
            json.dumps(redacted_data)
        except (TypeError, ValueError) as exc:
            raise ValueError("tool result data must be JSON serializable") from exc
        object.__setattr__(self, "data", redacted_data)

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_name": self.tool_name,
            "ok": self.ok,
            "data": self.data,
            "error_code": self.error_code,
            "message": self.message,
        }


ToolHandler = Callable[[BaseModel], ToolResult]


@dataclass(frozen=True)
class Tool:
    definition: ToolDefinition
    arguments_model: type[BaseModel]
    handler: ToolHandler


def profile_tool_definition(name: str, description: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        category="connection",
        risk_level="read_only",
        input_schema=ProfileNameArguments.model_json_schema(),
    )


def _redact_secret_values(value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes)):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(key): _redact_secret_values(redact_details(item, str(key)))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_values(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secret_values(item) for item in value]
    return redact_details(value)
