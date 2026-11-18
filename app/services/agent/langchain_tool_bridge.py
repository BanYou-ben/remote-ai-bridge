"""Safety-preserving bridge from LangChain tools to the RAB registry.

This module does not start an agent or connect to a model.  Callers must
provide a per-run execution context and an output sanitizer.
"""
from __future__ import annotations

import json
from collections.abc import Callable

from langchain_core.tools import StructuredTool

from app.services.agent.tool_registry import ToolRegistry


class LangChainToolBridge:
    def __init__(
        self,
        registry: ToolRegistry,
        profile_name: str,
        sanitize: Callable[[str, dict], dict],
        *,
        profile_alias: str = "selected_profile",
        allowed_names: frozenset[str] | None = None,
    ) -> None:
        self.registry = registry
        self.profile_name = profile_name
        self.profile_alias = profile_alias
        self.sanitize = sanitize
        self.allowed_names = allowed_names
        self.targets: dict[str, str] = {}

    def build_tools(self) -> list[StructuredTool]:
        tools: list[StructuredTool] = []
        for definition in self.registry.list_definitions():
            if definition.risk_level != "read_only":
                continue
            if self.allowed_names is not None and definition.name not in self.allowed_names:
                continue
            registered = self.registry.get(definition.name)
            if "profile_name" not in registered.arguments_model.model_fields:
                continue
            tools.append(StructuredTool.from_function(
                func=self._handler(definition.name),
                name=definition.name,
                description=definition.description,
                args_schema=registered.arguments_model,
            ))
        return tools

    def _handler(self, name: str):
        def execute(**kwargs):
            def denied(code):
                safe = self.sanitize(name, {"ok": False, "data": {}, "error_code": code})
                safe["error_code"] = code
                return json.dumps(safe, ensure_ascii=True)

            # Re-check the authoritative registry at execution time.
            try:
                registered = self.registry.get(name)
            except LookupError:
                return denied("TOOL_NOT_FOUND")
            if registered.definition.risk_level != "read_only":
                return denied("AGENT_TOOL_NOT_ALLOWED")
            if self.allowed_names is not None and name not in self.allowed_names:
                return denied("AGENT_TOOL_NOT_ALLOWED")
            if kwargs.get("profile_name") != self.profile_alias:
                return denied("AGENT_PROFILE_SCOPE_VIOLATION")
            args = dict(kwargs)
            args["profile_name"] = self.profile_name
            if "target_name" in args:
                target = self.targets.get(args["target_name"])
                if target is None:
                    return denied("APPLICATION_TARGET_INVALID")
                args["target_name"] = target
            result = self.registry.execute(name, args)
            if name == "list_application_targets" and result.ok:
                safe_targets = []
                for target in result.data.get("targets", [])[:100]:
                    if not isinstance(target, dict) or not isinstance(target.get("name"), str):
                        continue
                    alias = next((k for k, v in self.targets.items() if v == target["name"]), None)
                    if alias is None:
                        alias = f"target_{len(self.targets) + 1}"
                        self.targets[alias] = target["name"]
                    safe_targets.append({"name": alias, "has_service": target.get("has_service") is True,
                                         "has_health": target.get("has_health") is True})
                payload = result.to_dict()
                payload["data"] = {"targets": safe_targets}
                return json.dumps(self.sanitize(name, payload), ensure_ascii=True)
            payload = result.to_dict()
            if "target_name" in kwargs:
                for item in payload.get("data", {}).get("evidence", []):
                    if isinstance(item, dict):
                        expected = f"application.{args['target_name']}."
                        if isinstance(item.get("id"), str) and item["id"].startswith(expected):
                            item["id"] = f"application.{kwargs['target_name']}." + item["id"][len(expected):]
            return json.dumps(self.sanitize(name, payload), ensure_ascii=True)

        return execute
