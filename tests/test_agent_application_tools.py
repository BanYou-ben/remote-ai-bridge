from __future__ import annotations

from dataclasses import dataclass, field
import json

import pytest
from pydantic import ValidationError

from app.domain.application_target import ApplicationTarget
from app.domain.errors import RABError
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.infrastructure.application_target_store import ApplicationTargetStore
from app.services.agent.agent_contract import (
    AgentRequest,
    AgentState,
    FinalDiagnosisDecision,
    ToolCallDecision,
)
from app.services.agent.agent_runner import AgentRunner
from app.services.agent.application_tools import (
    ProfileTargetToolArguments,
    build_application_tools,
)
from app.services.agent.tool_contract import (
    ProfileNameArguments,
    Tool,
    ToolResult,
    profile_scoped_tool_definition,
)
from app.services.agent.tool_registry import ToolRegistry
from app.services.ai.diagnosis import StructuredDiagnosis
from app.services.application_diagnostics import (
    ApplicationHealthReport,
    ApplicationServiceReport,
)
from app.services.application_target_service import ApplicationTargetService


def _profile(name: str = "server-a") -> Profile:
    return Profile(
        schema_version=2,
        name=name,
        ssh_target="alice@ssh.example.test",
        profile_type="managed",
        host="ssh.example.test",
        username="alice",
        ssh_port=2222,
        key_id="1" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:" + "A" * 43,
    )


def _target(
    name: str = "rab-api",
    profile_name: str = "server-a",
    *,
    service_unit: str | None = "rab-api.service",
    health_url: str | None = "http://127.0.0.1:8000/health",
) -> ApplicationTarget:
    return ApplicationTarget(1, name, profile_name, service_unit, health_url)


class FakeProfiles:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, name: str) -> Profile:
        self.calls.append(name)
        return _profile(name)


class FakeTargets:
    def __init__(self, targets: list[ApplicationTarget]) -> None:
        self.targets = targets
        self.calls: list[str] = []
        self.error: RABError | None = None

    def list(self) -> list[ApplicationTarget]:
        self.calls.append("list")
        if self.error is not None:
            raise self.error
        return list(self.targets)

    def get(self, name: str) -> ApplicationTarget:
        self.calls.append(f"get:{name}")
        if self.error is not None:
            raise self.error
        for target in self.targets:
            if target.name == name:
                return target
        raise RABError("APPLICATION_TARGET_NOT_FOUND", "sensitive path")


class FakeDiagnostics:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.service_check = CheckResult(
            "Application service",
            CheckStatus.PASS,
            "application service is active",
        )
        self.health_check = CheckResult(
            "Application health",
            CheckStatus.FAIL,
            "application health endpoint returned HTTP 500",
            "APPLICATION_HEALTH_UNHEALTHY",
            500,
        )

    def check_service(
        self,
        profile: Profile,
        target: ApplicationTarget,
    ) -> ApplicationServiceReport:
        self.calls.append(f"service:{profile.name}:{target.name}")
        return ApplicationServiceReport(
            self.service_check,
            target.name,
            "active" if self.service_check.status is CheckStatus.PASS else None,
            "running" if self.service_check.status is CheckStatus.PASS else None,
        )

    def check_health(
        self,
        profile: Profile,
        target: ApplicationTarget,
    ) -> ApplicationHealthReport:
        self.calls.append(f"health:{profile.name}:{target.name}")
        return ApplicationHealthReport(
            self.health_check,
            target.name,
            self.health_check.http_status,
        )


def _registry(
    targets: list[ApplicationTarget] | None = None,
) -> tuple[ToolRegistry, FakeProfiles, FakeTargets, FakeDiagnostics]:
    profiles = FakeProfiles()
    target_service = FakeTargets(targets or [_target()])
    diagnostics = FakeDiagnostics()
    registry = ToolRegistry()
    for tool in build_application_tools(profiles, target_service, diagnostics):
        registry.register(tool)
    return registry, profiles, target_service, diagnostics


def test_application_target_arguments_are_strict_and_forbid_extra_fields() -> None:
    parsed = ProfileTargetToolArguments(
        profile_name="server-a",
        target_name="rab-api",
    )
    assert parsed.target_name == "rab-api"
    with pytest.raises(ValidationError):
        ProfileTargetToolArguments.model_validate(
            {"profile_name": "server-a", "target_name": "rab-api", "url": "http://evil"},
            strict=True,
        )
    with pytest.raises(ValidationError):
        ProfileTargetToolArguments.model_validate(
            {"profile_name": "server-a", "target_name": 1},
            strict=True,
        )


def test_list_application_targets_is_profile_scoped_and_hides_configuration() -> None:
    registry, profiles, targets, diagnostics = _registry(
        [_target(), _target("private-b", "server-b")]
    )
    result = registry.execute(
        "list_application_targets",
        {"profile_name": "server-a"},
    )
    assert result.ok is True
    assert result.data == {
        "targets": [{"name": "rab-api", "has_service": True, "has_health": True}]
    }
    serialized = json.dumps(result.to_dict())
    assert "rab-api.service" not in serialized
    assert "127.0.0.1" not in serialized
    assert profiles.calls == ["server-a"]
    assert targets.calls == ["list"]
    assert diagnostics.calls == []


@pytest.mark.parametrize(
    ("tool_name", "suffix"),
    [
        ("check_application_service", "service"),
        ("check_application_health", "health"),
    ],
)
def test_application_diagnostic_tools_emit_target_specific_evidence(
    tool_name: str,
    suffix: str,
) -> None:
    registry, _, _, diagnostics = _registry()
    result = registry.execute(
        tool_name,
        {"profile_name": "server-a", "target_name": "rab-api"},
    )
    assert result.ok is True
    evidence = result.data["evidence"][0]
    assert evidence["id"] == f"application.rab-api.{suffix}"
    assert evidence["category"] == "application"
    assert result.data["metrics"]["target_name"] == "rab-api"
    assert diagnostics.calls == [f"{suffix}:server-a:rab-api"]


def test_target_not_found_is_safe_tool_failure() -> None:
    registry, _, targets, diagnostics = _registry()
    targets.error = RABError(
        "APPLICATION_TARGET_NOT_FOUND",
        "C:/Users/private/application_targets/missing.json",
    )
    result = registry.execute(
        "check_application_health",
        {"profile_name": "server-a", "target_name": "missing"},
    )
    assert result.ok is False
    assert result.error_code == "APPLICATION_TARGET_NOT_FOUND"
    assert "C:/Users" not in result.message
    assert diagnostics.calls == []


def test_invalid_persisted_target_is_rejected_without_diagnostics() -> None:
    registry, _, targets, diagnostics = _registry()
    targets.error = RABError(
        "APPLICATION_TARGET_INVALID",
        "health_url=http://169.254.169.254:80/",
    )
    result = registry.execute(
        "check_application_health",
        {"profile_name": "server-a", "target_name": "rab-api"},
    )
    assert result.ok is False
    assert result.error_code == "APPLICATION_TARGET_INVALID"
    assert "169.254" not in result.message
    assert diagnostics.calls == []


@pytest.mark.parametrize(
    "health_url",
    [
        "http://169.254.169.254:80/",
        "http://example.com:80/",
        "http://10.0.0.1:8000/",
        "http://user:pass@127.0.0.1:8000/",
    ],
)
def test_corrupted_persisted_target_never_reaches_application_diagnostics(
    tmp_path,
    health_url: str,
) -> None:
    profiles = FakeProfiles()
    store = ApplicationTargetStore(tmp_path)
    store.targets_dir.mkdir(parents=True)
    payload = _target().to_dict()
    payload["health_url"] = health_url
    (store.targets_dir / "rab-api.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    diagnostics = FakeDiagnostics()
    registry = ToolRegistry()
    target_service = ApplicationTargetService(store, profiles)
    for tool in build_application_tools(profiles, target_service, diagnostics):
        registry.register(tool)

    result = registry.execute(
        "check_application_health",
        {"profile_name": "server-a", "target_name": "rab-api"},
    )

    assert result.ok is False
    assert result.error_code == "APPLICATION_TARGET_INVALID"
    assert diagnostics.calls == []
    assert profiles.calls == []


def test_target_profile_mismatch_never_loads_profile_or_runs_diagnostics() -> None:
    registry, profiles, _, diagnostics = _registry([_target("app-b", "server-b")])
    result = registry.execute(
        "check_application_health",
        {"profile_name": "server-a", "target_name": "app-b"},
    )
    assert result.ok is False
    assert result.error_code == "APPLICATION_TARGET_PROFILE_MISMATCH"
    assert profiles.calls == []
    assert diagnostics.calls == []


def test_persisted_target_name_mismatch_is_rejected_before_diagnostics(tmp_path) -> None:
    profiles = FakeProfiles()
    store = ApplicationTargetStore(tmp_path)
    store.targets_dir.mkdir(parents=True)
    (store.targets_dir / "rab-api.json").write_text(
        json.dumps(_target("different").to_dict()), encoding="utf-8"
    )
    diagnostics = FakeDiagnostics()
    registry = ToolRegistry()
    for tool in build_application_tools(
        profiles, ApplicationTargetService(store, profiles), diagnostics
    ):
        registry.register(tool)
    result = registry.execute(
        "check_application_health",
        {"profile_name": "server-a", "target_name": "rab-api"},
    )
    assert result.error_code == "APPLICATION_TARGET_INVALID"
    assert not result.ok
    assert profiles.calls == []
    assert diagnostics.calls == []


@pytest.mark.parametrize(
    ("tool_name", "check", "expected_code"),
    [
        (
            "check_application_service",
            CheckResult(
                "Application service",
                CheckStatus.SKIP,
                "not configured",
                "APPLICATION_SERVICE_NOT_CONFIGURED",
            ),
            "APPLICATION_SERVICE_NOT_CONFIGURED",
        ),
        (
            "check_application_health",
            CheckResult(
                "Application health",
                CheckStatus.SKIP,
                "not configured",
                "APPLICATION_HEALTH_NOT_CONFIGURED",
            ),
            "APPLICATION_HEALTH_NOT_CONFIGURED",
        ),
    ],
)
def test_not_configured_capability_is_grounded_skip_evidence(
    tool_name: str,
    check: CheckResult,
    expected_code: str,
) -> None:
    registry, _, _, diagnostics = _registry()
    if tool_name.endswith("service"):
        diagnostics.service_check = check
    else:
        diagnostics.health_check = check
    result = registry.execute(
        tool_name,
        {"profile_name": "server-a", "target_name": "rab-api"},
    )
    evidence = result.data["evidence"][0]
    assert result.ok is True
    assert evidence["status"] == "SKIP"
    assert evidence["error_code"] == expected_code


@dataclass
class ScriptedModel:
    decisions: list[object]
    states: list[AgentState] = field(default_factory=list)

    def decide(self, request, state, tools):
        self.states.append(state)
        return self.decisions.pop(0)


def _diagnosis(
    evidence_ids: tuple[str, ...] = ("application.rab-api.health",),
    stage: str = "application.health",
) -> StructuredDiagnosis:
    return StructuredDiagnosis(
        summary="The trusted application health endpoint is unhealthy.",
        diagnosis_stage=stage,
        evidence_ids=evidence_ids,
        possible_causes=("The application returned an unhealthy status.",),
        recommended_actions=("Inspect the application without changing it.",),
        confidence="high",
    )


def test_agent_runner_rejects_cross_profile_application_tool_before_handler() -> None:
    registry, profiles, targets, diagnostics = _registry([_target("app-b", "server-b")])
    model = ScriptedModel(
        [
            ToolCallDecision(
                "check_application_health",
                {"profile_name": "server-b", "target_name": "app-b"},
            )
        ]
    )
    result = AgentRunner(registry, model, max_steps=1).run(
        AgentRequest("server-a", "Check application")
    )
    assert result.observations[0].error_code == "AGENT_PROFILE_SCOPE_VIOLATION"
    assert profiles.calls == []
    assert targets.calls == []
    assert diagnostics.calls == []


def test_agent_target_binding_is_second_profile_scope_layer() -> None:
    registry, profiles, targets, diagnostics = _registry([_target("app-b", "server-b")])
    model = ScriptedModel(
        [
            ToolCallDecision(
                "check_application_health",
                {"profile_name": "server-a", "target_name": "app-b"},
            )
        ]
    )
    result = AgentRunner(registry, model, max_steps=1).run(
        AgentRequest("server-a", "Check application")
    )
    assert result.observations[0].error_code == "APPLICATION_TARGET_PROFILE_MISMATCH"
    assert targets.calls == ["get:app-b"]
    assert profiles.calls == []
    assert diagnostics.calls == []


def test_agent_completes_application_service_and_health_diagnosis() -> None:
    registry, _, _, diagnostics = _registry()
    model = ScriptedModel(
        [
            ToolCallDecision("list_application_targets", {"profile_name": "server-a"}),
            ToolCallDecision(
                "check_application_service",
                {"profile_name": "server-a", "target_name": "rab-api"},
            ),
            ToolCallDecision(
                "check_application_health",
                {"profile_name": "server-a", "target_name": "rab-api"},
            ),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    result = AgentRunner(registry, model).run(
        AgentRequest("server-a", "Why is my API not working?")
    )
    assert result.status == "completed"
    assert result.step_count == 3
    assert diagnostics.calls == ["service:server-a:rab-api", "health:server-a:rab-api"]
    assert result.diagnosis is not None
    assert result.diagnosis.diagnosis_stage == "application.health"
    assert result.diagnosis.evidence_ids == ("application.rab-api.health",)


def _network_tool() -> Tool:
    def handler(arguments: ProfileNameArguments) -> ToolResult:
        return ToolResult(
            "check_ssh_transport",
            True,
            {
                "evidence": [
                    {
                        "id": "network.ssh.tcp",
                        "category": "network",
                        "name": "SSH transport",
                        "status": "PASS",
                        "detail": "SSH TCP endpoint is reachable",
                        "error_code": None,
                        "http_status": None,
                    }
                ]
            },
        )

    return Tool(
        profile_scoped_tool_definition(
            "check_ssh_transport",
            "Check SSH transport.",
            category="network",
        ),
        ProfileNameArguments,
        handler,
    )


def test_cross_domain_agent_grounds_application_diagnosis_in_application_evidence() -> None:
    registry, _, _, _ = _registry()
    registry.register(_network_tool())
    model = ScriptedModel(
        [
            ToolCallDecision("check_ssh_transport", {"profile_name": "server-a"}),
            ToolCallDecision(
                "check_application_health",
                {"profile_name": "server-a", "target_name": "rab-api"},
            ),
            FinalDiagnosisDecision(_diagnosis()),
        ]
    )
    result = AgentRunner(registry, model).run(
        AgentRequest("server-a", "Diagnose network and application")
    )
    assert result.status == "completed"
    assert {item.category for item in model.states[-1].evidence} == {
        "network",
        "application",
    }


def test_cross_domain_agent_rejects_application_stage_with_only_network_evidence() -> None:
    registry, _, _, _ = _registry()
    registry.register(_network_tool())
    model = ScriptedModel(
        [
            ToolCallDecision("check_ssh_transport", {"profile_name": "server-a"}),
            FinalDiagnosisDecision(
                _diagnosis(("network.ssh.tcp",), stage="application.health")
            ),
        ]
    )
    result = AgentRunner(registry, model).run(
        AgentRequest("server-a", "Diagnose application")
    )
    assert result.status == "failed"
    assert result.error_code == "AGENT_DIAGNOSIS_INVALID"


def test_application_tools_expose_no_agent_controlled_remote_parameters() -> None:
    registry, _, _, _ = _registry()
    allowed = {
        "list_application_targets": {"profile_name"},
        "check_application_service": {"profile_name", "target_name"},
        "check_application_health": {"profile_name", "target_name"},
    }
    for definition in registry.list_definitions():
        properties = set(definition.input_schema["properties"])
        assert properties == allowed[definition.name]
        assert properties.isdisjoint({"service", "url", "host", "port", "path", "command"})
        assert definition.risk_level == "read_only"
