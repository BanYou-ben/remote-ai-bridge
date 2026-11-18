"""Minimized, profile-scoped model view for local Web diagnostics."""
from __future__ import annotations

import re
import math
import threading
from app.redaction import redact
from app.services.agent.agent_contract import AgentObservation, AgentRequest, AgentState, ToolCallDecision
from app.services.agent.agent_runner import AgentRunner
from app.services.ai.schemas import DiagnosticEvidence


ALIAS = "selected_profile"
DIAGNOSIS_GATE = threading.BoundedSemaphore(1)
ALLOWED_TOOLS = frozenset({
    "get_profile_summary", "list_application_targets", "check_application_service", "check_application_health",
    "get_runtime_status", "run_doctor", "check_local_proxy", "check_ssh_config",
    "check_ssh_transport", "check_tunnel_process", "check_remote_listener",
    "check_remote_endpoint", "get_remote_system_info", "check_remote_memory",
    "check_remote_disk", "check_remote_load", "get_remote_top_processes",
    "check_remote_failed_services",
})
LEGACY_ALLOWED_TOOLS = ALLOWED_TOOLS - {"get_profile_summary", "list_application_targets", "check_application_service", "check_application_health"}
EVIDENCE_IDS = frozenset({
    "connection.proxy.tcp", "connection.proxy.handshake", "connection.proxy.endpoint",
    "connection.ssh.config", "connection.tunnel.process", "connection.remote.listener",
    "connection.remote.endpoint", "network.ssh.dns", "network.ssh.tcp",
    "system.remote.os", "system.remote.memory", "system.remote.disk",
    "system.remote.load", "system.remote.processes", "system.remote.services",
})


def create_deepseek_model(secret: str):
    from langchain_deepseek import ChatDeepSeek

    class DeepSeekCompatibleChat(ChatDeepSeek):
        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            # LangChain ToolStrategy requests "any" (serialized as "required").
            # DeepSeek rejected that value; local output and tool checks still apply.
            if tool_choice in ("any", "required"):
                tool_choice = "auto"
            return super().bind_tools(tools, tool_choice=tool_choice, **kwargs)

    return DeepSeekCompatibleChat(model="deepseek-flash", api_key=secret,
                        base_url="https://api.deepseek.com", timeout=30, max_retries=0,
                        max_tokens=1024)


def safe_question(value: str, secret: str = "") -> str:
    value = redact(value)
    if secret:
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)\b(?:sk-[\w-]{8,}|ghp_[\w]+|github_pat_[\w]+)\b", "[REDACTED]", value)
    value = re.sub(r"https?://[^\s]+", "[URL]", value, flags=re.I)
    value = re.sub(r"(?<!\w)(?:[A-Za-z]:\\|/home/|/Users/)[^\s]*", "[PATH]", value)
    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]", value)
    return "".join(c if c.isprintable() else " " for c in value)[:500]


def _safe_evidence(item: DiagnosticEvidence) -> DiagnosticEvidence | None:
    if item.id not in EVIDENCE_IDS and not re.fullmatch(r"application\.target_[1-9][0-9]*\.(?:service|health)", item.id):
        return None
    if item.category != item.id.split(".", 1)[0] or item.status not in {"PASS", "FAIL", "SKIP"}:
        return None
    return DiagnosticEvidence(
        id=item.id, category=item.id.split(".", 1)[0], name=item.id,
        status=item.status, detail="Diagnostic check result", error_code=None,
        http_status=item.http_status if item.http_status in (200, 401, 403) else None,
    )


def _safe_metrics(name: str, data: dict) -> dict[str, object]:
    if name == "get_runtime_status":
        safe: dict[str, object] = {}
        state = data.get("state")
        if isinstance(state, str) and state in {"READY", "STARTING", "CONNECTING", "DEGRADED", "FAILED", "STOPPING", "STOPPED", "UNSUPERVISED"}:
            safe["state"] = state
        for field in ("supervised", "runtime_present", "process_alive"):
            if type(data.get(field)) is bool:
                safe[field] = data[field]
        return safe
    allowed = {
        "check_remote_memory": ("available_percent",),
        "check_remote_disk": ("used_percent",),
        "check_remote_load": ("load_1m_per_cpu", "cpu_count"),
    }
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        return {}
    result: dict[str, object] = {}
    for name in allowed.get(name, ()):
        value = metrics.get(name)
        if type(value) in (int, float) and 0 <= value <= 1_000_000 and math.isfinite(value):
            result[name] = value
    return result


class MinimizedAgentModel:
    def __init__(self, model, profile_name: str, secret: str) -> None:
        self.model = model
        self.profile_name = profile_name
        self.secret = secret

    def decide(self, request, state, tools):
        evidence = tuple(safe for item in state.evidence if (safe := _safe_evidence(item)))
        observations = []
        for item in state.observations:
            if item.tool_name not in LEGACY_ALLOWED_TOOLS:
                continue
            raw = item.data.get("evidence", [])
            ids = {entry.get("id") for entry in raw if isinstance(entry, dict)} if isinstance(raw, list) else set()
            safe_items = [entry.to_dict() for entry in evidence if entry.id in ids]
            data: dict[str, object] = {}
            if safe_items:
                data["evidence"] = safe_items
            metrics = _safe_metrics(item.tool_name, item.data)
            if metrics:
                data["metrics"] = metrics
            observations.append(AgentObservation(
                item.tool_name, {"profile_name": ALIAS}, item.ok,
                data, None, "Tool completed" if item.ok else "Tool failed",
            ))
        safe_state = AgentState(state.step_count, tuple(observations), evidence)
        question = safe_question(request.message, self.secret).replace(self.profile_name, "[PROFILE]")
        safe_request = AgentRequest(ALIAS, question)
        safe_tools = tuple(tool for tool in tools if tool.name in LEGACY_ALLOWED_TOOLS)
        decision = self.model.decide(safe_request, safe_state, safe_tools)
        if isinstance(decision, ToolCallDecision):
            # Reject unexposed names before the registry can see them.
            if decision.tool_name not in LEGACY_ALLOWED_TOOLS:
                return ToolCallDecision("unavailable_tool", {})
            arguments = dict(decision.arguments)
            if arguments.get("profile_name") == ALIAS:
                arguments["profile_name"] = self.profile_name
            return ToolCallDecision(decision.tool_name, arguments)
        return decision


def run_web_diagnostics(registry, model, profile_name: str, question: str, secret: str, *, max_model_calls: int = 6):
    # Keep the older model protocol available to existing local validation callers.
    # The production DeepSeek factory always returns a LangChain chat model.
    if callable(getattr(model, "decide", None)) and not callable(getattr(model, "bind_tools", None)):
        return AgentRunner(registry, MinimizedAgentModel(model, profile_name, secret), max_steps=6).run(
            AgentRequest(profile_name, question)
        )
    from app.services.agent.langchain_runtime import run_langchain_diagnostics
    return run_langchain_diagnostics(registry, model, profile_name, question, secret, max_model_calls=max_model_calls)


def public_result(result, secret: str) -> dict[str, object]:
    allowed_evidence = {}
    for observation in result.observations:
        values = observation.data.get("evidence", [])
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and isinstance(item.get("id"), str) and (item["id"] in EVIDENCE_IDS or re.fullmatch(r"application\.target_[1-9][0-9]*\.(?:service|health)", item["id"])):
                    identifier = item["id"]
                    allowed_evidence[identifier] = {
                        "id": identifier, "status": item.get("status") if item.get("status") in ("PASS", "FAIL", "SKIP") else "SKIP",
                    }
    diagnosis = result.diagnosis
    return {
        "status": result.status,
        "error_code": result.error_code,
        "tools": [{"name": item.tool_name if item.tool_name in ALLOWED_TOOLS else "unknown", "ok": item.ok} for item in result.observations],
        "evidence": list(allowed_evidence.values()),
        "diagnosis": None if diagnosis is None else {
            "stage": safe_question(diagnosis.diagnosis_stage, secret),
            "summary": safe_question(diagnosis.summary, secret),
            "evidence_ids": [item for item in diagnosis.evidence_ids if item in allowed_evidence],
        },
    }
