from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from fastapi.testclient import TestClient

from app.services.agent.tool_contract import ProfileNameArguments, Tool, ToolResult, profile_tool_definition
from app.services.agent.tool_registry import ToolRegistry
from app.services.agent.web_diagnostics import run_web_diagnostics


class OfflineChat(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, arguments, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": call_id, "type": "tool_call"}])


def test_real_langchain_graph_multistep_grounded_diagnosis():
    registry = ToolRegistry()
    executed = []
    def doctor(args):
        executed.append(args.profile_name)
        return ToolResult("run_doctor", True, {"evidence": [{"id": "connection.ssh.config", "category": "connection", "name": "private", "status": "FAIL", "detail": "private", "error_code": None, "http_status": None}]})
    registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments, doctor))
    model = OfflineChat(responses=[
        call("run_doctor", {"profile_name": "selected_profile"}, "a"),
        call("DiagnosisOutput", {"summary": "SSH check failed", "diagnosis_stage": "connection.ssh", "evidence_ids": ["connection.ssh.config"], "possible_causes": [], "recommended_actions": [], "confidence": "low"}, "b"),
    ])
    result = run_web_diagnostics(registry, model, "server", "check connection", "fixture")
    assert executed == ["server"]
    assert result.status == "completed"
    assert result.diagnosis.evidence_ids == ("connection.ssh.config",)


def test_real_langchain_rejects_fabricated_evidence():
    model = OfflineChat(responses=[call("DiagnosisOutput", {"summary": "invented", "diagnosis_stage": "connection.ssh", "evidence_ids": ["connection.ssh.config"], "possible_causes": [], "recommended_actions": [], "confidence": "low"}, "a")])
    result = run_web_diagnostics(ToolRegistry(), model, "server", "check", "fixture")
    assert result.status == "failed"
    assert result.diagnosis is None


def test_real_langchain_rejects_parallel_calls_before_execution():
    registry = ToolRegistry()
    executed = []
    def doctor(args):
        executed.append(args.profile_name)
        return ToolResult("run_doctor", True, {})
    registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments, doctor))
    model = OfflineChat(responses=[AIMessage(content="", tool_calls=[
        {"name": "run_doctor", "args": {"profile_name": "selected_profile"}, "id": "a", "type": "tool_call"},
        {"name": "run_doctor", "args": {"profile_name": "selected_profile"}, "id": "b", "type": "tool_call"},
    ])])
    result = run_web_diagnostics(registry, model, "server", "check", "fixture")
    assert result.status == "failed"
    assert not executed


def test_fastapi_calls_real_langchain_and_returns_grounded_result(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from app.api.app import create_app
    registry = ToolRegistry()
    registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments,
        lambda args: ToolResult("run_doctor", True, {"evidence": [{"id": "connection.ssh.config", "category": "connection", "name": "private", "status": "FAIL", "detail": "private", "error_code": None, "http_status": None}]})))
    services = SimpleNamespace(profiles=MagicMock(), runtime_manager=MagicMock(), agent_tool_registry=registry)
    services.runtime_manager.shutdown.return_value = SimpleNamespace(completed=True)
    model = OfflineChat(responses=[
        call("run_doctor", {"profile_name": "selected_profile"}, "a"),
        call("DiagnosisOutput", {"summary": "SSH check failed", "diagnosis_stage": "connection.ssh", "evidence_ids": ["connection.ssh.config"], "possible_causes": [], "recommended_actions": [], "confidence": "low"}, "b"),
    ])
    monkeypatch.setattr("app.api.routes.create_deepseek_model", lambda secret: model)
    with TestClient(create_app(services=services), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/ai/diagnose/server", json={"question": "check", "api_key": "fixture", "consent": True})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["diagnosis"]["evidence_ids"] == ["connection.ssh.config"]
    assert "fixture" not in response.text


def test_bridge_exposes_all_eighteen_read_only_registry_tools():
    import inspect
    from unittest.mock import MagicMock
    from app.services.agent.registry_factory import build_agent_tool_registry
    from app.services.agent.langchain_tool_bridge import LangChainToolBridge
    from app.services.agent.web_diagnostics import ALLOWED_TOOLS
    services = {key: MagicMock() for key in inspect.signature(build_agent_tool_registry).parameters}
    registry = build_agent_tool_registry(**services)
    tools = LangChainToolBridge(registry, "server", lambda name, result: result, allowed_names=ALLOWED_TOOLS).build_tools()
    assert len(tools) == 18
    assert {tool.name for tool in tools} == {item.name for item in registry.list_definitions()}
    assert all(item.risk_level == "read_only" for item in registry.list_definitions())
    for tool in tools:
        # Every tool requires a profile; invalid arguments cannot reach a handler.
        assert "profile_name" in tool.args_schema.model_fields
        args = {"profile_name": "other_profile"}
        if "target_name" in tool.args_schema.model_fields:
            args["target_name"] = "target_1"
        assert "AGENT_PROFILE_SCOPE_VIOLATION" in tool.invoke(args)
    assert all(not service.mock_calls for service in services.values())


def test_real_langchain_rejects_cross_profile_without_executing():
    registry = ToolRegistry()
    executed = []
    def doctor(args):
        executed.append(args.profile_name)
        return ToolResult("run_doctor", True, {})
    registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments, doctor))
    model = OfflineChat(responses=[call("run_doctor", {"profile_name": "another_profile"}, "a"),
        call("DiagnosisOutput", {"summary": "bad", "diagnosis_stage": "unknown", "evidence_ids": ["connection.ssh.config"], "possible_causes": [], "recommended_actions": [], "confidence": "low"}, "b")])
    result = run_web_diagnostics(registry, model, "server", "check", "fixture")
    assert result.status == "failed"
    assert not executed


def test_real_langchain_timeout_is_safe():
    class TimeoutChat(OfflineChat):
        def _generate(self, *args, **kwargs):
            raise TimeoutError("private fixture secret")
    result = run_web_diagnostics(ToolRegistry(), TimeoutChat(responses=[]), "server", "check", "fixture")
    assert result.status == "failed"
    assert result.error_code == "AGENT_MODEL_TIMEOUT"
    assert "secret" not in result.message


def test_real_langchain_model_call_limit_stops_repeated_tools():
    registry = ToolRegistry()
    executed = []
    def doctor(args):
        executed.append(args.profile_name)
        return ToolResult("run_doctor", True, {})
    registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments, doctor))
    model = OfflineChat(responses=[call("run_doctor", {"profile_name": "selected_profile"}, str(i)) for i in range(6)])
    result = run_web_diagnostics(registry, model, "server", "check", "fixture")
    assert result.error_code == "AGENT_MAX_STEPS"
    assert 1 <= len(executed) <= 6


def test_real_chatdeepseek_and_langchain_use_mock_http_transport():
    import json
    import httpx
    from langchain_deepseek import ChatDeepSeek
    requests = []
    def send(request):
        requests.append(json.loads(request.content))
        name = "run_doctor" if len(requests) == 1 else "DiagnosisOutput"
        args = {"profile_name": "selected_profile"} if len(requests) == 1 else {
            "summary": "SSH check failed", "diagnosis_stage": "connection.ssh", "evidence_ids": ["connection.ssh.config"],
            "possible_causes": [], "recommended_actions": [], "confidence": "low"}
        body = {"id": "fixture", "object": "chat.completion", "created": 1, "model": "deepseek-flash", "choices": [{
            "index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}
        return httpx.Response(200, json=body, request=request)
    registry = ToolRegistry()
    registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments,
        lambda args: ToolResult("run_doctor", True, {"evidence": [{"id": "connection.ssh.config", "category": "connection", "name": "private-host", "status": "FAIL", "detail": "private-host", "error_code": None, "http_status": None}]})))
    with httpx.Client(transport=httpx.MockTransport(send)) as client:
        model = ChatDeepSeek(model="deepseek-flash", api_key="fixture", base_url="https://provider.invalid", http_client=client, max_retries=0)
        result = run_web_diagnostics(registry, model, "private-profile", "check private-profile", "fixture")
    assert result.status == "completed"
    assert len(requests) == 2
    assert result.diagnosis.evidence_ids == ("connection.ssh.config",)
    sent = json.dumps(requests)
    assert "private-profile" not in sent
    assert "private-host" not in sent


def test_real_chatdeepseek_reports_safe_http_error_classification():
    import httpx
    from langchain_deepseek import ChatDeepSeek
    for status, expected in ((400, "AGENT_MODEL_BAD_REQUEST_MODEL"), (401, "AGENT_MODEL_AUTH_FAILED"),
                             (402, "AGENT_MODEL_PAYMENT_REQUIRED"), (429, "AGENT_MODEL_RATE_LIMITED"),
                             (500, "AGENT_MODEL_SERVER_ERROR")):
        def send(request):
            return httpx.Response(status, json={"error": {"message": "model invalid private-secret", "param": "model"}}, request=request)
        with httpx.Client(transport=httpx.MockTransport(send)) as client:
            model = ChatDeepSeek(model="deepseek-flash", api_key="fixture-secret", base_url="https://provider.invalid", http_client=client, max_retries=0)
            result = run_web_diagnostics(ToolRegistry(), model, "server", "check", "fixture-secret")
        assert result.error_code == expected
        assert "private-secret" not in result.message
        assert "fixture-secret" not in result.message


def test_production_factory_uses_auto_tool_choice_with_real_langchain():
    import json
    import httpx2
    from app.services.agent.web_diagnostics import create_deepseek_model
    requests = []
    def send(request):
        payload = json.loads(request.content)
        requests.append(payload)
        name = "run_doctor" if len(requests) == 1 else "DiagnosisOutput"
        args = {"profile_name": "selected_profile"} if len(requests) == 1 else {
            "summary": "SSH check failed", "diagnosis_stage": "connection.ssh", "evidence_ids": ["connection.ssh.config"],
            "possible_causes": [], "recommended_actions": [], "confidence": "low"}
        return httpx2.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{"index": 0, "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{len(requests)}",
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}, request=request)
    registry = ToolRegistry()
    registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments,
        lambda args: ToolResult("run_doctor", True, {"evidence": [{"id": "connection.ssh.config", "category": "connection",
            "name": "fixture", "status": "PASS", "detail": "fixture", "error_code": None, "http_status": None}]})))
    model = create_deepseek_model("fixture-key")
    model.root_client._client._mounts = {}
    model.root_client._client._transport = httpx2.MockTransport(send)
    try:
        result = run_web_diagnostics(registry, model, "server", "check", "fixture-key")
    finally:
        model.root_client.close()
    assert result.status == "completed", (result.error_code, len(requests))
    assert len(requests) == 2
    assert all(payload.get("tool_choice") == "auto" for payload in requests)
    assert result.diagnosis.evidence_ids == ("connection.ssh.config",)
