from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import httpx
from openai import OpenAI

from app.api.app import create_app
from app.services.agent.agent_contract import AgentModelError, AgentObservation, AgentRequest, AgentState, FinalDiagnosisDecision, ToolCallDecision
from app.services.agent.tool_contract import ProfileNameArguments, Tool, ToolResult, profile_tool_definition
from app.services.agent.tool_registry import ToolRegistry
from app.services.agent.web_diagnostics import DIAGNOSIS_GATE, MinimizedAgentModel, _safe_metrics
from app.infrastructure.ai.openai_compatible_agent_model import OpenAICompatibleResponsesAgentModel
from app.services.ai.diagnosis import StructuredDiagnosis


def _services():
    services = SimpleNamespace(profiles=MagicMock(), runtime_manager=MagicMock(), agent_tool_registry=ToolRegistry())
    services.runtime_manager.shutdown.return_value = SimpleNamespace(completed=True)
    def handler(arguments):
        return ToolResult("run_doctor", True, {"evidence": [{"id": "connection.ssh.config", "category": "connection", "name": "private-host", "status": "FAIL", "detail": "password=secret private-host", "error_code": "PRIVATE", "http_status": None}]})
    services.agent_tool_registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments, handler))
    return services


class FakeModel:
    states = []
    def __init__(self, *args, **kwargs):
        self.calls = 0

    def decide(self, request, state, tools):
        self.calls += 1
        self.states.append((request, state, tools))
        if self.calls == 1:
            return ToolCallDecision("run_doctor", {"profile_name": "selected_profile"})
        return FinalDiagnosisDecision(StructuredDiagnosis(
            "SSH check failed", "connection.ssh", ("connection.ssh.config",), (), (), "low"
        ))


def test_web_agent_minimizes_observation_and_returns_grounded_evidence(monkeypatch):
    FakeModel.states = []
    monkeypatch.setattr("app.api.routes.create_deepseek_model", FakeModel)
    services = _services()
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/ai/diagnose/private-profile", json={"question": "check 10.20.30.40", "api_key": "fixture-secret", "consent": True})
    assert response.status_code == 200
    assert response.json()["diagnosis"]["evidence_ids"] == ["connection.ssh.config"]
    assert response.json()["evidence"] == [{"id": "connection.ssh.config", "status": "FAIL"}]
    request, state, _ = FakeModel.states[-1]
    sent = json.dumps({"request": request.to_dict(), "state": state.to_dict()})
    for private in ("fixture-secret", "private-host", "private-profile", "10.20.30.40", "password"):
        assert private not in sent
        assert private not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_web_agent_requires_consent_and_rejects_remote_origin(monkeypatch):
    monkeypatch.setattr("app.api.routes.create_deepseek_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call model")))
    services = _services()
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8000") as client:
        no_consent = client.post("/ai/diagnose/server", json={"question": "test", "api_key": "secret", "consent": False})
        remote = client.post("/ai/diagnose/server", headers={"origin": "https://evil.example"}, json={"question": "test", "api_key": "secret", "consent": True})
    assert no_consent.status_code == 400
    assert remote.status_code == 400
    services.profiles.get.assert_not_called()


def test_web_agent_model_timeout_does_not_stop_runtime(monkeypatch):
    class TimeoutModel:
        def __init__(self, *args, **kwargs):
            pass
        def decide(self, *args):
            raise AgentModelError("AGENT_MODEL_TIMEOUT", "timeout", retryable=True)
    monkeypatch.setattr("app.api.routes.create_deepseek_model", TimeoutModel)
    services = _services()
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/ai/diagnose/server", json={"question": "test", "api_key": "secret", "consent": True})
    assert response.json()["error_code"] == "AGENT_MODEL_TIMEOUT"
    services.runtime_manager.stop.assert_not_called()
    services.runtime_manager.start.assert_not_called()


def test_model_input_excludes_runtime_private_fields_and_unapproved_tools():
    class Capture:
        def decide(self, request, state, tools):
            self.request, self.state, self.tools = request, state, tools
            return ToolCallDecision("run_doctor", {"profile_name": "selected_profile"})
    model = Capture()
    wrapped = MinimizedAgentModel(model, "private-profile", "fixture-key")
    observation = AgentObservation("get_runtime_status", {"profile_name": "private-profile"}, True,
                                   {"state": "READY", "pid": 12345, "runtime_present": True, "message": "private-host"}, None, "private-host")
    tools = (profile_tool_definition("run_doctor", "Safe"), profile_tool_definition("get_profile_summary", "Private"))
    decision = wrapped.decide(AgentRequest("private-profile", "检查 private-profile"), AgentState(1, (observation,), ()), tools)
    serialized = json.dumps(model.state.to_dict(), ensure_ascii=False)
    assert "private-profile" not in model.request.message
    assert "private-host" not in serialized and "12345" not in serialized
    assert model.state.observations[0].data["metrics"]["state"] == "READY"
    assert [tool.name for tool in model.tools] == ["run_doctor"]
    assert decision.arguments["profile_name"] == "private-profile"


def test_unapproved_tool_and_cross_profile_cannot_execute():
    services = _services()
    class Malicious:
        def decide(self, request, state, tools):
            return ToolCallDecision("run_doctor", {"profile_name": "other"})
    from app.services.agent.web_diagnostics import run_web_diagnostics
    result = run_web_diagnostics(services.agent_tool_registry, Malicious(), "server", "diagnose", "fixture-key")
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"
    assert result.status == "failed"


def test_safe_metrics_drop_nonfinite_values_and_sensitive_fields():
    assert _safe_metrics("check_remote_memory", {"metrics": {"available_percent": 3.0, "host": "secret"}}) == {"available_percent": 3.0}
    assert _safe_metrics("check_remote_memory", {"metrics": {"available_percent": float('nan')}}) == {}
    assert _safe_metrics("get_remote_top_processes", {"metrics": {"processes": [{"name": "private"}]}}) == {}


def test_ai_gate_refuses_duplicate_run_before_model(monkeypatch):
    services = _services()
    monkeypatch.setattr("app.api.routes.create_deepseek_model", lambda *args: (_ for _ in ()).throw(AssertionError("must not run")))
    assert DIAGNOSIS_GATE.acquire(blocking=False)
    try:
        with TestClient(create_app(services=services), base_url="http://127.0.0.1:8000") as client:
            response = client.post("/ai/diagnose/server", json={"question": "test", "api_key": "secret", "consent": True})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "AI_DIAGNOSIS_BUSY"
    finally:
        DIAGNOSIS_GATE.release()


def test_agent_blocks_unapproved_registered_tool():
    services = _services()
    called = []
    services.agent_tool_registry.register(Tool(
        profile_tool_definition("get_profile_summary", "Private"), ProfileNameArguments,
        lambda arguments: called.append(arguments.profile_name),
    ))
    class Malicious:
        def decide(self, request, state, tools):
            return ToolCallDecision("get_profile_summary", {"profile_name": "selected_profile"})
    from app.services.agent.web_diagnostics import run_web_diagnostics
    result = run_web_diagnostics(services.agent_tool_registry, Malicious(), "server", "diagnose", "fixture-key")
    assert called == []
    assert result.observations[0].error_code == "TOOL_NOT_FOUND"


def test_web_to_real_adapter_http_mock_multistep_is_minimized(monkeypatch):
    requests = []
    diagnosis = {"summary": "SSH check failed", "diagnosis_stage": "connection.ssh", "evidence_ids": ["connection.ssh.config"], "possible_causes": [], "recommended_actions": [], "confidence": "low"}
    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        name = "run_doctor" if len(requests) == 1 else "submit_diagnosis"
        arguments = {"profile_name": "selected_profile"} if len(requests) == 1 else diagnosis
        body = {"id": "fixture", "object": "response", "created_at": 1, "model": "deepseek-flash", "status": "completed", "output": [{"type": "function_call", "id": "fixture", "call_id": "fixture", "name": name, "arguments": json.dumps(arguments), "status": "completed"}]}
        return httpx.Response(200, json=body, request=request)
    http_client = httpx.Client(transport=httpx.MockTransport(transport))
    sdk = OpenAI(api_key="fixture-only", base_url="https://provider.invalid", http_client=http_client, max_retries=0)
    monkeypatch.setattr("app.api.routes.create_deepseek_model", lambda key: OpenAICompatibleResponsesAgentModel("deepseek-flash", "https://provider.invalid", strict_tools=False, tool_choice="auto", client=sdk))
    services = _services()
    try:
        with TestClient(create_app(services=services), base_url="http://127.0.0.1:8000") as client:
            response = client.post("/ai/diagnose/private-profile", json={"question": "diagnose private-profile", "api_key": "fixture-secret", "consent": True})
    finally:
        sdk.close()
    assert response.json()["status"] == "completed"
    assert response.json()["diagnosis"]["evidence_ids"] == ["connection.ssh.config"]
    sent = json.dumps(requests)
    for private in ("fixture-secret", "private-profile", "private-host", "password=secret"):
        assert private not in sent
    payload = json.loads(requests[1]["input"])
    assert payload["TOOL_OBSERVATIONS"][0]["tool_name"] == "run_doctor"
    assert payload["COLLECTED_EVIDENCE"][0]["id"] == "connection.ssh.config"
