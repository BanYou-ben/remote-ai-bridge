from __future__ import annotations

from pathlib import Path

import pytest

from app.services.agent.tool_contract import ProfileNameArguments, Tool, ToolResult, profile_tool_definition
from app.services.agent.tool_registry import ToolNotFoundError, ToolRegistrationError, ToolRegistry


def _tool(name: str, handler=None) -> Tool:
    return Tool(
        profile_tool_definition(name, f"Description for {name}."),
        ProfileNameArguments,
        handler or (lambda args: ToolResult(name, True, {"profile": args.profile_name})),
    )


def test_register_and_get_tool() -> None:
    registry = ToolRegistry()
    tool = _tool("status")
    registry.register(tool)
    assert registry.get("status") is tool


def test_duplicate_registration_fails() -> None:
    registry = ToolRegistry()
    registry.register(_tool("status"))
    with pytest.raises(ToolRegistrationError):
        registry.register(_tool("status"))


def test_get_unknown_tool_fails() -> None:
    with pytest.raises(ToolNotFoundError):
        ToolRegistry().get("missing")


def test_definitions_are_sorted_deterministically() -> None:
    registry = ToolRegistry()
    registry.register(_tool("zeta"))
    registry.register(_tool("alpha"))
    assert [item.name for item in registry.list_definitions()] == ["alpha", "zeta"]


def test_execute_returns_structured_success() -> None:
    registry = ToolRegistry()
    registry.register(_tool("status"))
    result = registry.execute("status", {"profile_name": "server"})
    assert result.ok is True
    assert result.data == {"profile": "server"}


def test_execute_unknown_returns_safe_failure() -> None:
    result = ToolRegistry().execute("missing", {"profile_name": "server"})
    assert (result.ok, result.error_code) == (False, "TOOL_NOT_FOUND")


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"profile_name": ""},
        {"profile_name": 123},
        {"profile_name": "server", "extra": True},
    ],
)
def test_execute_rejects_invalid_arguments(arguments: dict[str, object]) -> None:
    registry = ToolRegistry()
    registry.register(_tool("status"))
    result = registry.execute("status", arguments)
    assert (result.ok, result.error_code) == (False, "TOOL_ARGUMENT_INVALID")


def test_argument_failure_does_not_call_handler() -> None:
    called = False

    def handler(_args):
        nonlocal called
        called = True
        return ToolResult("status", True, {})

    registry = ToolRegistry()
    registry.register(_tool("status", handler))
    registry.execute("status", {"profile_name": "bad name"})
    assert called is False


def test_handler_exception_is_isolated_and_secret_safe() -> None:
    def handler(_args):
        raise RuntimeError("password=RAB-SECRET")

    registry = ToolRegistry()
    registry.register(_tool("status", handler))
    result = registry.execute("status", {"profile_name": "server"})
    assert result.error_code == "TOOL_EXECUTION_FAILED"
    assert "RAB-SECRET" not in result.message


def test_invalid_handler_result_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(_tool("status", lambda _args: {"unsafe": True}))
    result = registry.execute("status", {"profile_name": "server"})
    assert result.error_code == "TOOL_EXECUTION_FAILED"


def test_mismatched_handler_tool_name_is_rejected_without_propagation() -> None:
    registry = ToolRegistry()
    registry.register(
        _tool(
            "get_runtime_status",
            lambda _args: ToolResult("run_doctor", True, {}),
        )
    )
    result = registry.execute("get_runtime_status", {"profile_name": "server"})
    assert result.ok is False
    assert result.tool_name == "get_runtime_status"
    assert result.error_code == "TOOL_RESULT_INVALID"


def test_matching_handler_tool_name_remains_successful() -> None:
    registry = ToolRegistry()
    registry.register(
        _tool(
            "get_runtime_status",
            lambda _args: ToolResult("get_runtime_status", True, {"state": "READY"}),
        )
    )
    result = registry.execute("get_runtime_status", {"profile_name": "server"})
    assert result.ok is True
    assert result.tool_name == "get_runtime_status"
    assert result.data == {"state": "READY"}


def test_agent_registry_has_no_provider_or_dynamic_execution_dependency() -> None:
    source = Path("app/services/agent/tool_registry.py").read_text(encoding="utf-8").lower()
    for forbidden in ("openai", "importlib", "subprocess", "shell=true", "eval(", "exec("):
        assert forbidden not in source
