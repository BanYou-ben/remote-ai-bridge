from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

from app.domain.application_target import (
    ApplicationTargetValidationError,
    validate_application_target_name,
)
from app.domain.errors import RABError
from app.domain.profile import ProfileValidationError, validate_profile_name
from app.services.agent.tool_contract import Tool, ToolDefinition, ToolResult
from app.services.ai.context_builder import build_diagnostic_evidence
from app.services.application_diagnostics import (
    ApplicationDiagnosticsService,
    ApplicationHealthReport,
    ApplicationServiceReport,
)
from app.services.application_target_service import ApplicationTargetService
from app.services.profile_service import ProfileService


class ProfileTargetToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile_name: StrictStr
    target_name: StrictStr

    @field_validator("profile_name")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        try:
            validate_profile_name(value)
        except ProfileValidationError as exc:
            raise ValueError("invalid profile name") from exc
        return value

    @field_validator("target_name")
    @classmethod
    def validate_target(cls, value: str) -> str:
        try:
            validate_application_target_name(value)
        except ApplicationTargetValidationError as exc:
            raise ValueError("invalid application target name") from exc
        return value


class ApplicationTargetListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile_name: StrictStr

    @field_validator("profile_name")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        try:
            validate_profile_name(value)
        except ProfileValidationError as exc:
            raise ValueError("invalid profile name") from exc
        return value


def build_application_tools(
    profiles: ProfileService,
    application_targets: ApplicationTargetService,
    application_diagnostics: ApplicationDiagnosticsService,
) -> tuple[Tool, ...]:
    def list_targets(arguments: BaseModel) -> ToolResult:
        if not isinstance(arguments, ApplicationTargetListArguments):
            raise TypeError("unexpected tool argument model")
        try:
            profiles.get(arguments.profile_name)
            targets = application_targets.list()
        except RABError as exc:
            return _target_error("list_application_targets", exc)
        return ToolResult(
            "list_application_targets",
            True,
            {
                "targets": [
                    {
                        "name": target.name,
                        "has_service": target.service_unit is not None,
                        "has_health": target.health_url is not None,
                    }
                    for target in targets
                    if target.profile_name == arguments.profile_name
                ]
            },
        )

    return (
        Tool(
            ToolDefinition(
                "list_application_targets",
                "List trusted application diagnostic targets for a profile.",
                "application",
                "read_only",
                ApplicationTargetListArguments.model_json_schema(),
            ),
            ApplicationTargetListArguments,
            list_targets,
        ),
        _diagnostic_tool(
            "check_application_service",
            "Check the configured service state for a trusted application target.",
            profiles,
            application_targets,
            application_diagnostics,
            capability="service",
        ),
        _diagnostic_tool(
            "check_application_health",
            "Check the configured loopback health endpoint for a trusted application target.",
            profiles,
            application_targets,
            application_diagnostics,
            capability="health",
        ),
    )


def _diagnostic_tool(
    name: str,
    description: str,
    profiles: ProfileService,
    application_targets: ApplicationTargetService,
    diagnostics: ApplicationDiagnosticsService,
    *,
    capability: str,
) -> Tool:
    def handler(arguments: BaseModel) -> ToolResult:
        if not isinstance(arguments, ProfileTargetToolArguments):
            raise TypeError("unexpected tool argument model")
        try:
            target = application_targets.get(arguments.target_name)
        except RABError as exc:
            return _target_error(name, exc)
        if target.name != arguments.target_name:
            return ToolResult(
                name,
                False,
                {},
                "APPLICATION_TARGET_INVALID",
                "application target is invalid",
            )
        if target.profile_name != arguments.profile_name:
            return ToolResult(
                name,
                False,
                {},
                "APPLICATION_TARGET_PROFILE_MISMATCH",
                "application target does not belong to the requested profile",
            )
        try:
            profile = profiles.get(arguments.profile_name)
        except RABError:
            return ToolResult(
                name,
                False,
                {},
                "APPLICATION_TARGET_PROFILE_NOT_FOUND",
                "application target profile does not exist",
            )
        report = (
            diagnostics.check_service(profile, target)
            if capability == "service"
            else diagnostics.check_health(profile, target)
        )
        evidence_id = f"application.{target.name}.{capability}"
        evidence = build_diagnostic_evidence(evidence_id, "application", report.check)
        return ToolResult(
            name,
            True,
            {
                "evidence": [evidence.to_dict()],
                "metrics": _safe_metrics(report),
            },
        )

    return Tool(
        ToolDefinition(
            name,
            description,
            "application",
            "read_only",
            ProfileTargetToolArguments.model_json_schema(),
        ),
        ProfileTargetToolArguments,
        handler,
    )


def _safe_metrics(
    report: ApplicationServiceReport | ApplicationHealthReport,
) -> dict[str, object]:
    metrics: dict[str, object] = {"target_name": report.target_name}
    if isinstance(report, ApplicationServiceReport):
        if report.active_state is not None:
            metrics["active_state"] = report.active_state
        if report.sub_state is not None:
            metrics["sub_state"] = report.sub_state
    elif report.http_status is not None:
        metrics["http_status"] = report.http_status
    return metrics


def _target_error(tool_name: str, error: RABError) -> ToolResult:
    safe_messages = {
        "APPLICATION_TARGET_NOT_FOUND": "application target was not found",
        "APPLICATION_TARGET_INVALID": "application target is invalid",
        "APPLICATION_TARGET_PROFILE_NOT_FOUND": "application target profile does not exist",
        "PROFILE_NOT_FOUND": "application target profile does not exist",
    }
    code = error.code
    if code == "PROFILE_NOT_FOUND":
        code = "APPLICATION_TARGET_PROFILE_NOT_FOUND"
    return ToolResult(
        tool_name,
        False,
        {},
        code,
        safe_messages.get(error.code, "application target operation failed"),
    )
