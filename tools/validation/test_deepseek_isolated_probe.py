from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.infrastructure.ai.openai_compatible_agent_model import OpenAICompatibleResponsesAgentModel
from tools.validation import deepseek_isolated_probe as probe


def _call(name, arguments):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(arguments))


def _response(*calls):
    return SimpleNamespace(status="completed", output=list(calls))


class FakeResponses:
    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _setup(monkeypatch, items):
    fake = FakeResponses(items)
    monkeypatch.setattr("builtins.input", lambda prompt: "YES")
    monkeypatch.setattr(probe.getpass, "getpass", lambda prompt: "fixture-key-not-real")
    monkeypatch.setattr(probe, "OpenAICompatibleResponsesAgentModel", lambda **kwargs: OpenAICompatibleResponsesAgentModel(**kwargs, client=SimpleNamespace(responses=fake)))
    return fake


def test_isolated_probe_three_steps_sends_only_fixture_data(monkeypatch, capsys):
    diagnosis = {"summary": "fixture", "diagnosis_stage": "connection.ssh", "evidence_ids": [probe.EVIDENCE_ID], "possible_causes": [], "recommended_actions": [], "confidence": "high"}
    fake = _setup(monkeypatch, [
        _response(_call("get_runtime_status", {"profile_name": probe.FIXTURE_PROFILE})),
        _response(_call("run_doctor", {"profile_name": probe.FIXTURE_PROFILE})),
        _response(_call("submit_diagnosis", diagnosis)),
    ])
    assert probe.main() == 0
    assert len(fake.calls) == 3
    for request in fake.calls:
        assert request["tool_choice"] == "auto"
        for tool in request["tools"]:
            assert "strict" not in tool
            assert "pattern" not in json.dumps(tool["parameters"])
    diagnosis_schema = fake.calls[2]["tools"][-1]["parameters"]["properties"]
    assert "connection.ssh" in diagnosis_schema["diagnosis_stage"]["description"]
    assert "No spaces or hyphens" in diagnosis_schema["diagnosis_stage"]["description"]
    assert "diagnosis_stage must be" in fake.calls[2]["instructions"]
    assert diagnosis_schema["confidence"]["enum"] == ["low", "medium", "high"]
    assert diagnosis_schema["evidence_ids"]["items"]["enum"] == [probe.EVIDENCE_ID]
    third = json.loads(fake.calls[2]["input"])
    assert [x["tool_name"] for x in third["TOOL_OBSERVATIONS"]] == ["get_runtime_status", "run_doctor"]
    assert third["COLLECTED_EVIDENCE"][0]["id"] == probe.EVIDENCE_ID
    output = capsys.readouterr().out
    assert "API key received (hidden)" in output
    assert "fixture-key-not-real" not in output


def test_empty_key_is_reported_without_creating_model(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "YES")
    monkeypatch.setattr(probe.getpass, "getpass", lambda prompt: "")
    monkeypatch.setattr(probe, "OpenAICompatibleResponsesAgentModel", lambda **kwargs: pytest.fail("model must not be created"))
    assert probe.main() == 1
    assert "No API key was entered" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "no", "yes"])
def test_no_confirmation_causes_no_model_or_key_input(monkeypatch, answer):
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    monkeypatch.setattr(probe.getpass, "getpass", lambda prompt: pytest.fail("key must not be read"))
    monkeypatch.setattr(probe, "OpenAICompatibleResponsesAgentModel", lambda **kwargs: pytest.fail("model must not be created"))
    assert probe.main() == 1


def test_multiple_function_calls_fail_closed(monkeypatch, capsys):
    fake = _setup(monkeypatch, [_response(_call("get_runtime_status", {"profile_name": probe.FIXTURE_PROFILE}), _call("run_doctor", {"profile_name": probe.FIXTURE_PROFILE}))])
    assert probe.main() == 1
    assert len(fake.calls) == 1
    assert "AGENT_MODEL_MULTIPLE_TOOL_CALLS" in capsys.readouterr().out


def test_provider_error_is_safe(monkeypatch, capsys):
    fake = _setup(monkeypatch, [TimeoutError("fixture-key-not-real")])
    assert probe.main() == 1
    assert len(fake.calls) == 1
    output = capsys.readouterr().out
    assert "AGENT_MODEL_TIMEOUT" in output
    assert "fixture-key-not-real" not in output


@pytest.mark.parametrize(("last", "expected"), [
    (_response(), "AGENT_MODEL_NO_TOOL_CALL"),
    (_response(_call("submit_diagnosis", {"summary": "invalid secret-fixture"})), "AGENT_MODEL_INVALID_DIAGNOSIS_FIELDS"),
    (_response(_call("not_a_tool", {})), "AGENT_MODEL_UNKNOWN_TOOL"),
])
def test_final_step_reports_safe_specific_failure(monkeypatch, capsys, last, expected):
    fake = _setup(monkeypatch, [
        _response(_call("get_runtime_status", {"profile_name": probe.FIXTURE_PROFILE})),
        _response(_call("run_doctor", {"profile_name": probe.FIXTURE_PROFILE})),
        last,
    ])
    assert probe.main() == 1
    assert len(fake.calls) == 3
    output = capsys.readouterr().out
    assert expected in output
    assert "secret-fixture" not in output


@pytest.mark.parametrize(("field", "value", "expected"), [
    ("summary", "", "AGENT_MODEL_INVALID_DIAGNOSIS_SUMMARY"),
    ("diagnosis_stage", "Bad Stage", "AGENT_MODEL_INVALID_DIAGNOSIS_STAGE"),
    ("evidence_ids", [], "AGENT_MODEL_INVALID_DIAGNOSIS_EVIDENCE"),
    ("possible_causes", "not a list", "AGENT_MODEL_INVALID_DIAGNOSIS_LISTS"),
    ("confidence", "certain", "AGENT_MODEL_INVALID_DIAGNOSIS_CONFIDENCE"),
])
def test_final_diagnosis_invalid_field_reports_only_category(monkeypatch, capsys, field, value, expected):
    diagnosis = {"summary": "safe", "diagnosis_stage": "connection.ssh", "evidence_ids": [probe.EVIDENCE_ID], "possible_causes": [], "recommended_actions": [], "confidence": "high"}
    diagnosis[field] = value
    _setup(monkeypatch, [
        _response(_call("get_runtime_status", {"profile_name": probe.FIXTURE_PROFILE})),
        _response(_call("run_doctor", {"profile_name": probe.FIXTURE_PROFILE})),
        _response(_call("submit_diagnosis", diagnosis)),
    ])
    assert probe.main() == 1
    assert expected in capsys.readouterr().out
