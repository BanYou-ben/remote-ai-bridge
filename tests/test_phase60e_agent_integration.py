from __future__ import annotations

from unittest.mock import MagicMock

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.services.agent.agent_contract import (
    AgentModelError,
    AgentRequest,
    FinalDiagnosisDecision,
    ToolCallDecision,
)
from app.services.agent.agent_runner import AgentRunner
from app.services.agent.registry_factory import build_agent_tool_registry
from app.services.ai.diagnosis import StructuredDiagnosis
from app.services.doctor import DoctorReport
from app.services.local_proxy import LocalProxyReport
from app.services.network_transport import NetworkTransportReport


def _profile() -> Profile:
    return Profile(schema_version=1, name="fixture", ssh_target="fixture")


def _check(name: str, status: CheckStatus, code: str | None = None) -> CheckResult:
    return CheckResult(name, status, "fixture result", code)


def _registry():
    deps = {name: MagicMock() for name in (
        "profiles", "runtime_manager", "doctor", "local_proxy", "ssh_config",
        "tunnel_manager", "remote_probe", "network_transport", "remote_system",
        "remote_runtime", "application_targets", "application_diagnostics",
    )}
    deps["profiles"].get.return_value = _profile()
    return build_agent_tool_registry(**deps), deps


class _ScriptedModel:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.states = []

    def decide(self, request, state, tools):
        self.states.append(state)
        decision = self.decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        return decision


def _diagnosis(stage: str, evidence_id: str) -> FinalDiagnosisDecision:
    return FinalDiagnosisDecision(StructuredDiagnosis(
        summary="Fixture diagnostic result",
        diagnosis_stage=stage,
        evidence_ids=(evidence_id,),
        possible_causes=(),
        recommended_actions=(),
        confidence="low",
    ))


def test_registry_agent_uses_ssh_failure_and_doctor_evidence_without_mutation():
    registry, deps = _registry()
    ok = _check("fixture", CheckStatus.PASS)
    failed = _check("SSH TCP", CheckStatus.FAIL, "SSH_TCP_UNREACHABLE")
    deps["network_transport"].check_ssh_transport.return_value = NetworkTransportReport(ok, failed)
    deps["doctor"].run.return_value = DoctorReport(
        LocalProxyReport(ok, ok, ok), ok,
        _check("Tunnel", CheckStatus.FAIL, "NO_RUNTIME_STATE"),
        _check("Listener", CheckStatus.SKIP),
        _check("Endpoint", CheckStatus.SKIP),
    )
    model = _ScriptedModel([
        ToolCallDecision("check_ssh_transport", {"profile_name": "fixture"}),
        ToolCallDecision("run_doctor", {"profile_name": "fixture"}),
        _diagnosis("connection.tunnel", "connection.tunnel.process"),
    ])
    result = AgentRunner(registry, model).run(AgentRequest("fixture", "diagnose"))
    assert result.status == "completed"
    assert model.states[-1].evidence
    assert result.observations[0].data["evidence"][1]["status"] == "FAIL"
    assert result.observations[1].data["evidence"][4]["error_code"] == "NO_RUNTIME_STATE"
    deps["tunnel_manager"].disconnect.assert_not_called()
    deps["runtime_manager"].start.assert_not_called()
    deps["runtime_manager"].stop.assert_not_called()


def test_registry_agent_handles_tool_exception_without_leaking_or_stopping_doctor():
    registry, deps = _registry()
    deps["network_transport"].check_ssh_transport.side_effect = RuntimeError("password=fixture-secret")
    ok = _check("fixture", CheckStatus.PASS)
    deps["doctor"].run.return_value = DoctorReport(LocalProxyReport(ok, ok, ok), ok, ok, ok, ok)
    model = _ScriptedModel([
        ToolCallDecision("check_ssh_transport", {"profile_name": "fixture"}),
        ToolCallDecision("run_doctor", {"profile_name": "fixture"}),
        _diagnosis("connection.ssh", "connection.ssh.config"),
    ])
    result = AgentRunner(registry, model).run(AgentRequest("fixture", "diagnose"))
    assert result.status == "completed"
    assert result.observations[0].error_code == "TOOL_EXECUTION_FAILED"
    assert "fixture-secret" not in str(result.to_dict())
    deps["doctor"].run.assert_called_once()


def test_registry_agent_timeout_preserves_error_and_does_not_run_tools():
    registry, deps = _registry()
    model = _ScriptedModel([AgentModelError("AGENT_MODEL_TIMEOUT", "timed out", retryable=True)])
    result = AgentRunner(registry, model).run(AgentRequest("fixture", "diagnose"))
    assert result.error_code == "AGENT_MODEL_TIMEOUT"
    assert result.observations == ()
    deps["doctor"].run.assert_not_called()
    deps["runtime_manager"].start.assert_not_called()


def test_registry_agent_rejects_final_diagnosis_without_evidence():
    registry, deps = _registry()
    model = _ScriptedModel([_diagnosis("connection.ssh", "connection.ssh.config")])
    result = AgentRunner(registry, model).run(AgentRequest("fixture", "diagnose"))
    assert result.error_code == "AGENT_DIAGNOSIS_INVALID"
    deps["doctor"].run.assert_not_called()
