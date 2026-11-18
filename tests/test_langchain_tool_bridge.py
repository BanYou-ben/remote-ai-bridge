from __future__ import annotations

import json

from app.services.agent.langchain_tool_bridge import LangChainToolBridge
from app.services.agent.tool_contract import ProfileNameArguments, Tool, ToolResult, profile_tool_definition
from app.services.agent.tool_registry import ToolRegistry


def test_real_langchain_tool_dispatches_through_registry_with_profile_scope():
    calls = []
    registry = ToolRegistry()

    def handler(arguments):
        calls.append(arguments.profile_name)
        return ToolResult("run_doctor", True, {"private": "fixture-host", "state": "PASS"})

    registry.register(Tool(profile_tool_definition("run_doctor", "Run read-only diagnostics"), ProfileNameArguments, handler))
    bridge = LangChainToolBridge(registry, "private-profile", lambda name, result: {"ok": result["ok"]})
    tool = bridge.build_tools()[0]
    assert json.loads(tool.invoke({"profile_name": "selected_profile"})) == {"ok": True}
    assert calls == ["private-profile"]
    assert json.loads(tool.invoke({"profile_name": "other"}))["error_code"] == "AGENT_PROFILE_SCOPE_VIOLATION"
    assert calls == ["private-profile"]
