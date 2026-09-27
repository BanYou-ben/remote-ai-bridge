from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from app.services.agent.tool_contract import Tool, ToolDefinition, ToolResult


class ToolRegistrationError(ValueError):
    pass


class ToolNotFoundError(LookupError):
    pass


class ToolArgumentError(ValueError):
    pass


class ToolExecutionError(RuntimeError):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.definition.name
        if name in self._tools:
            raise ToolRegistrationError(f"tool is already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"tool is not registered: {name}") from exc

    def list_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._tools[name].definition for name in sorted(self._tools))

    def execute(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        try:
            tool = self.get(name)
        except ToolNotFoundError:
            return ToolResult(
                tool_name=name,
                ok=False,
                data={},
                error_code="TOOL_NOT_FOUND",
                message="requested tool is not available",
            )
        try:
            validated = tool.arguments_model.model_validate(dict(arguments), strict=True)
        except (ValidationError, TypeError, ValueError):
            return ToolResult(
                tool_name=name,
                ok=False,
                data={},
                error_code="TOOL_ARGUMENT_INVALID",
                message="tool arguments are invalid",
            )
        try:
            result = tool.handler(validated)
        except Exception:
            return ToolResult(
                tool_name=name,
                ok=False,
                data={},
                error_code="TOOL_EXECUTION_FAILED",
                message="tool execution failed",
            )
        if not isinstance(result, ToolResult):
            return ToolResult(
                tool_name=name,
                ok=False,
                data={},
                error_code="TOOL_EXECUTION_FAILED",
                message="tool execution returned an invalid result",
            )
        if result.tool_name != name:
            return ToolResult(
                tool_name=name,
                ok=False,
                data={},
                error_code="TOOL_RESULT_INVALID",
                message="tool execution returned an invalid result",
            )
        return result
