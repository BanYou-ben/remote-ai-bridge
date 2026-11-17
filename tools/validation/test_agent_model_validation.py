from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.infrastructure.ai.openai_compatible_agent_model import OpenAICompatibleResponsesAgentModel
from app.services.agent.tool_contract import ProfileNameArguments, Tool, ToolResult, profile_tool_definition
from app.services.agent.tool_registry import ToolRegistry
from tools.validation import agent_model_validation as validation


EVIDENCE = {
    "id": "connection.tunnel.process",
    "category": "connection",
    "name": "Tunnel",
    "status": "FAIL",
    "detail": "process exited",
    "error_code": "PROCESS_EXITED",
    "http_status": None,
}


def call(name, arguments):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments))


def response(*calls):
    return SimpleNamespace(status="completed", output=list(calls))


class FakeResponses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def setup_validation(monkeypatch, responses, calls):
    monkeypatch.setattr("builtins.input", lambda prompt: "YES")
    registry = ToolRegistry()

    def handler(arguments):
        calls.append(arguments.profile_name)
        return ToolResult("run_doctor", True, {"evidence": [EVIDENCE]})

    registry.register(Tool(profile_tool_definition("run_doctor", "Diagnose"), ProfileNameArguments, handler))
    profiles = SimpleNamespace(get=lambda name: object())
    monkeypatch.setattr(validation, "create_services", lambda: SimpleNamespace(profiles=profiles, agent_tool_registry=registry))
    fake = FakeResponses(responses)
    client = SimpleNamespace(responses=fake)
    monkeypatch.setattr(
        validation,
        "OpenAICompatibleResponsesAgentModel",
        lambda **kwargs: OpenAICompatibleResponsesAgentModel(**kwargs, client=client),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "FAKE_PRIVATE_KEY_TEST")
    return fake


def args():
    return ["--profile", "server", "--question", "diagnose", "--max-steps", "4"]


def test_fake_multistep_responses_returns_grounded_diagnosis(monkeypatch, capsys):
    calls = []
    diagnosis = {
        "summary": "Tunnel exited",
        "diagnosis_stage": "connection.tunnel",
        "evidence_ids": ["connection.tunnel.process"],
        "possible_causes": [],
        "recommended_actions": [],
        "confidence": "high",
    }
    fake = setup_validation(monkeypatch, [response(call("run_doctor", {"profile_name": "server"})), response(call("submit_diagnosis", diagnosis))], calls)
    assert validation.main(args()) == 0
    assert calls == ["server"]
    second = json.loads(fake.requests[1]["input"])
    assert second["TOOL_OBSERVATIONS"][0]["tool_name"] == "run_doctor"
    assert second["COLLECTED_EVIDENCE"][0]["id"] == EVIDENCE["id"]
    assert fake.requests[0]["model"] == "deepseek-flash"
    output = capsys.readouterr().out
    assert '"status": "completed"' in output
    assert '"name": "run_doctor"' in output
    assert '"ok": true' in output
    assert '"stage": "connection.tunnel"' in output
    assert '"connection.tunnel.process"' in output
    assert '"summary": "Tunnel exited"' in output
    assert "FAKE_PRIVATE_KEY_TEST" not in output


def test_multiple_function_calls_fail_without_executing_any(monkeypatch, capsys):
    calls = []
    setup_validation(monkeypatch, [response(call("run_doctor", {"profile_name": "server"}), call("run_doctor", {"profile_name": "server"}))], calls)
    assert validation.main(args()) == 1
    assert calls == []
    assert "AGENT_MODEL_INVALID_RESPONSE" in capsys.readouterr().out


@pytest.mark.parametrize("error", [TimeoutError("FAKE_PRIVATE_KEY_TEST"), RuntimeError("FAKE_PRIVATE_KEY_TEST")])
def test_provider_errors_are_safe(monkeypatch, capsys, error):
    calls = []
    setup_validation(monkeypatch, [error], calls)
    assert validation.main(args()) == 1
    assert calls == []
    assert "FAKE_PRIVATE_KEY_TEST" not in capsys.readouterr().out


def test_cli_rejects_key_argument_and_out_of_bounds_steps():
    with pytest.raises(SystemExit):
        validation.main(args() + ["--api-key", "fake"])
    with pytest.raises(SystemExit):
        validation.main(["--profile", "server", "--question", "q", "--max-steps", "13"])


def test_transient_key_prompt_and_startup_errors_do_not_leak(monkeypatch, capsys):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "YES")
    monkeypatch.setattr(validation.getpass, "getpass", lambda prompt: "FAKE_PRIVATE_KEY_TEST")
    def fail():
        raise RuntimeError("FAKE_PRIVATE_KEY_TEST")
    monkeypatch.setattr(validation, "create_services", fail)
    assert validation.main(args()) == 1
    assert "FAKE_PRIVATE_KEY_TEST" not in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "no", "yes"])
def test_confirmation_declined_never_creates_services_or_calls_api(monkeypatch, capsys, answer):
    calls = []
    fake = setup_validation(monkeypatch, [], calls)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    monkeypatch.setattr(validation, "create_services", lambda: pytest.fail("must not create services"))
    assert validation.main(args()) == 1
    assert fake.requests == []
    assert calls == []
    output = capsys.readouterr().out
    assert "deepseek-flash" in output
    assert "charges" in output
    assert "question, tool observations and Evidence" in output
    assert "DeepSeek" in output


def test_summary_redacts_known_key_secrets_paths_and_bounds_length(monkeypatch, capsys):
    calls = []
    diagnosis = {
        "summary": "FAKE_PRIVATE_KEY_TEST password=verysecret http://host/path /home/alice/key 10.2.3.4 " + "x" * 800,
        "diagnosis_stage": "connection.tunnel",
        "evidence_ids": ["connection.tunnel.process"],
        "possible_causes": [],
        "recommended_actions": [],
        "confidence": "high",
    }
    setup_validation(monkeypatch, [response(call("run_doctor", {"profile_name": "server"})), response(call("submit_diagnosis", diagnosis))], calls)
    assert validation.main(args()) == 0
    output = capsys.readouterr().out
    for secret in ("FAKE_PRIVATE_KEY_TEST", "verysecret", "http://host/path", "/home/alice/key", "10.2.3.4"):
        assert secret not in output
    payload = json.loads(output.splitlines()[-1])
    assert len(payload["diagnosis"]["summary"]) <= validation.MAX_SUMMARY_LENGTH
    assert "[REDACTED]" in payload["diagnosis"]["summary"]
    assert "detail" not in payload["tools"][0]


def test_eof_at_confirmation_cancels_before_api(monkeypatch, capsys):
    calls = []
    fake = setup_validation(monkeypatch, [], calls)
    def eof(prompt):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert validation.main(args()) == 1
    assert fake.requests == []
    assert "cancelled" in capsys.readouterr().out
