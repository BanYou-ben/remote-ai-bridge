from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from app.services.agent.tool_contract import (
    ProfileNameArguments,
    ToolDefinition,
    ToolResult,
    profile_tool_definition,
)


def test_tool_definition_is_json_safe() -> None:
    definition = profile_tool_definition("inspect", "Inspect a profile.")
    assert json.loads(json.dumps(definition.to_dict()))["name"] == "inspect"


def test_tool_definition_contains_required_metadata() -> None:
    definition = profile_tool_definition("inspect", "Inspect a profile.")
    assert (definition.category, definition.risk_level) == ("connection", "read_only")


def test_profile_argument_schema_is_strict_and_closed() -> None:
    schema = ProfileNameArguments.model_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["profile_name"]
    assert schema["properties"]["profile_name"]["type"] == "string"


@pytest.mark.parametrize("value", ["", " has-space", "name/child", "-leading"])
def test_profile_name_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValidationError):
        ProfileNameArguments.model_validate({"profile_name": value})


@pytest.mark.parametrize("value", [None, 7, True, 7.0])
def test_profile_name_rejects_non_strings(value: object) -> None:
    with pytest.raises(ValidationError):
        ProfileNameArguments.model_validate({"profile_name": value}, strict=True)


def test_profile_arguments_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ProfileNameArguments.model_validate({"profile_name": "server", "password": "secret"})


def test_tool_result_is_json_safe() -> None:
    result = ToolResult("inspect", True, {"count": 1, "items": ["a"]})
    assert json.loads(json.dumps(result.to_dict()))["data"]["count"] == 1


def test_tool_result_redacts_message_and_nested_data() -> None:
    result = ToolResult(
        "inspect",
        False,
        {"auth_token": "top-secret", "nested": {"password": "pw"}},
        "FAILED",
        "password=top-secret",
    )
    assert "top-secret" not in json.dumps(result.to_dict())
    assert result.data["auth_token"] == "[REDACTED]"


def test_tool_result_redacts_secret_str_values() -> None:
    result = ToolResult("inspect", True, {"value": SecretStr("RAB-SECRET")})
    assert result.data == {"value": "[REDACTED]"}


def test_tool_result_rejects_non_json_data() -> None:
    with pytest.raises(ValueError, match="JSON serializable"):
        ToolResult("inspect", True, {"value": object()})


def test_definition_dataclass_preserves_explicit_schema() -> None:
    definition = ToolDefinition("x", "y", "connection", "read_only", {"type": "object"})
    assert definition.to_dict()["input_schema"] == {"type": "object"}
