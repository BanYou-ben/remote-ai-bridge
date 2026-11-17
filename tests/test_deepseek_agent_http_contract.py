from __future__ import annotations

import json

import httpx
import pytest
from openai import OpenAI

from app.infrastructure.ai.openai_compatible_agent_model import OpenAICompatibleResponsesAgentModel
from app.services.agent.agent_contract import AgentRequest
from app.services.agent.agent_runner import AgentRunner
from app.services.agent.tool_contract import ProfileNameArguments, Tool, ToolResult, profile_tool_definition
from app.services.agent.tool_registry import ToolRegistry


def _response(calls):
    return {"id": "resp_fixture", "object": "response", "created_at": 1, "model": "deepseek-flash", "status": "completed", "output": calls, "parallel_tool_calls": True}


def _call(name, arguments, index=1):
    return {"type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}", "name": name, "arguments": json.dumps(arguments), "status": "completed"}


def _registry(calls):
    registry = ToolRegistry()
    def handler(arguments):
        calls.append(arguments.profile_name)
        return ToolResult("run_doctor", True, {"evidence": [{"id": "connection.ssh.config", "category": "connection", "name": "SSH", "status": "PASS", "detail": "configuration available", "error_code": None, "http_status": None}]})
    registry.register(Tool(profile_tool_definition("run_doctor", "Inspect connection"), ProfileNameArguments, handler))
    return registry


def _run(responses, calls, requests):
    def transport(request):
        requests.append(json.loads(request.content))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body, request=request)
    http = httpx.Client(transport=httpx.MockTransport(transport))
    with OpenAI(api_key="fixture-only", base_url="https://provider.invalid", http_client=http, max_retries=0) as client:
        model = OpenAICompatibleResponsesAgentModel("deepseek-flash", "https://provider.invalid", client=client)
        return AgentRunner(_registry(calls), model).run(AgentRequest("server", "Check connection"))


def test_sdk_http_transport_multistep_returns_grounded_diagnosis():
    calls, requests = [], []
    diagnosis = {"summary": "SSH configuration is available", "diagnosis_stage": "connection.ssh", "evidence_ids": ["connection.ssh.config"], "possible_causes": [], "recommended_actions": [], "confidence": "high"}
    result = _run([(200, _response([_call("run_doctor", {"profile_name": "server"})])), (200, _response([_call("submit_diagnosis", diagnosis)]))], calls, requests)
    assert result.status == "completed"
    assert calls == ["server"]
    payload = json.loads(requests[1]["input"])
    assert payload["TOOL_OBSERVATIONS"][0]["tool_name"] == "run_doctor"
    assert payload["COLLECTED_EVIDENCE"][0]["id"] == "connection.ssh.config"
    assert result.diagnosis.evidence_ids == ("connection.ssh.config",)
    assert requests[0]["tool_choice"] == "required"


def test_sdk_http_multiple_calls_are_rejected_before_execution():
    calls, requests = [], []
    result = _run([(200, _response([_call("run_doctor", {"profile_name": "server"}), _call("run_doctor", {"profile_name": "server"}, 2)]))], calls, requests)
    assert result.error_code == "AGENT_MODEL_INVALID_RESPONSE"
    assert calls == []


def test_sdk_http_cross_profile_is_rejected_without_execution():
    calls, requests = [], []
    result = _run([(200, _response([_call("run_doctor", {"profile_name": "other"})])), (200, _response([]))], calls, requests)
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"
    assert calls == []


@pytest.mark.parametrize(("status", "expected"), [
    (400, "AGENT_MODEL_BAD_REQUEST"),
    (401, "AGENT_MODEL_AUTH_FAILED"),
    (402, "AGENT_MODEL_PAYMENT_REQUIRED"),
    (403, "AGENT_MODEL_ACCESS_DENIED"),
    (404, "AGENT_MODEL_ENDPOINT_NOT_FOUND"),
    (422, "AGENT_MODEL_UNPROCESSABLE_REQUEST"),
    (429, "AGENT_MODEL_RATE_LIMITED"),
    (500, "AGENT_MODEL_SERVER_ERROR"),
])
def test_sdk_http_provider_errors_do_not_leak(status, expected):
    calls, requests = [], []
    result = _run([(status, {"error": {"message": "fixture-sensitive", "type": "error"}})], calls, requests)
    assert result.error_code == expected
    assert "fixture-sensitive" not in json.dumps(result.to_dict())
    assert calls == []


@pytest.mark.parametrize(("param", "expected"), [
    ("tools[2].parameters", "AGENT_MODEL_BAD_REQUEST_TOOLS"),
    ("model", "AGENT_MODEL_BAD_REQUEST_MODEL"),
    ("private-secret-value", "AGENT_MODEL_BAD_REQUEST"),
])
def test_bad_request_param_is_allowlisted_and_never_echoed(param, expected):
    calls, requests = [], []
    result = _run([(400, {"error": {"message": "private-secret-value", "type": "invalid_request_error", "param": param}})], calls, requests)
    assert result.error_code == expected
    assert "private-secret-value" not in json.dumps(result.to_dict())
    assert calls == []


@pytest.mark.parametrize(("message", "expected"), [
    ("schema invalid secret-fixture", "AGENT_MODEL_BAD_REQUEST_SCHEMA"),
    ("tool_choice invalid secret-fixture", "AGENT_MODEL_BAD_REQUEST_TOOL_CHOICE"),
    ("strict invalid secret-fixture", "AGENT_MODEL_BAD_REQUEST_STRICT"),
    ("tools invalid secret-fixture", "AGENT_MODEL_BAD_REQUEST_TOOLS"),
    ("model invalid secret-fixture", "AGENT_MODEL_BAD_REQUEST_MODEL"),
    ("unrecognized secret-fixture", "AGENT_MODEL_BAD_REQUEST"),
])
def test_bad_request_message_returns_only_safe_category(message, expected):
    calls, requests = [], []
    result = _run([(400, {"error": {"message": message, "type": "invalid_request_error"}})], calls, requests)
    assert result.error_code == expected
    assert "secret-fixture" not in json.dumps(result.to_dict())
    assert calls == []


def test_specific_safe_error_category_takes_precedence_over_generic_tools_param():
    calls, requests = [], []
    result = _run([(400, {"error": {"message": "tools schema invalid secret-fixture", "param": "tools"}})], calls, requests)
    assert result.error_code == "AGENT_MODEL_BAD_REQUEST_SCHEMA"
    assert "secret-fixture" not in json.dumps(result.to_dict())
    assert calls == []


def test_sdk_http_unexpected_tool_type_rejects_entire_response():
    calls, requests = [], []
    extra = {"type": "custom_tool_call", "id": "custom_1", "call_id": "custom_call_1", "name": "apply_patch", "input": "test"}
    result = _run([(200, _response([_call("run_doctor", {"profile_name": "server"}), extra]))], calls, requests)
    assert result.error_code == "AGENT_MODEL_INVALID_RESPONSE"
    assert calls == []


def test_sdk_http_unobserved_evidence_is_rejected():
    calls, requests = [], []
    diagnosis = {"summary": "invented", "diagnosis_stage": "connection.ssh", "evidence_ids": ["connection.ssh.config"], "possible_causes": [], "recommended_actions": [], "confidence": "high"}
    result = _run([(200, _response([_call("submit_diagnosis", diagnosis)]))], calls, requests)
    assert result.error_code == "AGENT_MODEL_INVALID_RESPONSE"
    assert calls == []
