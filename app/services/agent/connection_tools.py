from __future__ import annotations

from pydantic import BaseModel

from app.redaction import redact
from app.services.agent.tool_contract import (
    ProfileNameArguments,
    Tool,
    ToolResult,
    profile_tool_definition,
)
from app.services.ai.context_builder import build_doctor_evidence
from app.services.ai.schemas import DiagnosticProfileSummary, DiagnosticRuntimeSummary
from app.services.doctor import DoctorService
from app.services.profile_service import ProfileService
from app.services.runtime_manager import RuntimeManager


def build_connection_tools(
    runtime_manager: RuntimeManager,
    profile_service: ProfileService,
    doctor_service: DoctorService,
) -> tuple[Tool, ...]:
    def runtime_status(arguments: BaseModel) -> ToolResult:
        parsed = _profile_arguments(arguments)
        snapshot = runtime_manager.status(parsed.profile_name)
        summary = DiagnosticRuntimeSummary(
            state=snapshot.state.value,
            attempt=snapshot.attempt,
            error_code=snapshot.error_code,
            message=snapshot.message,
            retry_in_seconds=snapshot.retry_in_seconds,
            remote_port=snapshot.remote_port,
            last_successful_probe_at=snapshot.last_successful_probe_at,
            suggested_port=snapshot.suggested_port,
            supervised=snapshot.supervised,
            runtime_present=snapshot.runtime_present,
            process_alive=snapshot.process_alive,
        )
        return ToolResult("get_runtime_status", True, summary.to_dict())

    def run_doctor(arguments: BaseModel) -> ToolResult:
        parsed = _profile_arguments(arguments)
        profile = profile_service.get(parsed.profile_name)
        report = doctor_service.run(profile)
        evidence = build_doctor_evidence(report)
        return ToolResult(
            "run_doctor",
            True,
            {"evidence": [item.to_dict() for item in evidence]},
        )

    def profile_summary(arguments: BaseModel) -> ToolResult:
        parsed = _profile_arguments(arguments)
        profile = profile_service.get(parsed.profile_name)
        summary = DiagnosticProfileSummary(
            name=profile.name,
            profile_type=profile.profile_type,
            local_proxy_host=profile.local_proxy_host,
            local_proxy_port=profile.local_proxy_port,
            remote_bind_host=profile.remote_bind_host,
            remote_port=profile.remote_port,
            auto_reconnect=profile.auto_reconnect,
            endpoint_probe_url=redact(profile.endpoint_probe_url),
        )
        return ToolResult("get_profile_summary", True, summary.to_dict())

    return (
        Tool(
            profile_tool_definition(
                "get_runtime_status",
                "Return the safe runtime supervision summary for a profile.",
            ),
            ProfileNameArguments,
            runtime_status,
        ),
        Tool(
            profile_tool_definition(
                "run_doctor",
                "Run read-only connection diagnostics and return grounded evidence.",
            ),
            ProfileNameArguments,
            run_doctor,
        ),
        Tool(
            profile_tool_definition(
                "get_profile_summary",
                "Return the safe non-credential configuration summary for a profile.",
            ),
            ProfileNameArguments,
            profile_summary,
        ),
    )


def _profile_arguments(arguments: BaseModel) -> ProfileNameArguments:
    if not isinstance(arguments, ProfileNameArguments):
        raise TypeError("unexpected tool argument model")
    return arguments
