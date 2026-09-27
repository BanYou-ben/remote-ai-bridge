from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from app.domain.profile import Profile
from app.services.agent.tool_contract import (
    ProfileNameArguments,
    Tool,
    ToolResult,
    profile_scoped_tool_definition,
)
from app.services.ai.context_builder import build_diagnostic_evidence
from app.services.profile_service import ProfileService
from app.services.remote_runtime import (
    RemoteLoadReport,
    RemoteProcessSnapshot,
    RemoteRuntimeService,
    RemoteServiceReport,
)


def build_runtime_tools(
    profile_service: ProfileService,
    remote_runtime: RemoteRuntimeService,
) -> tuple[Tool, ...]:
    return (
        _runtime_tool(
            "check_remote_load",
            "Check remote load averages against the configured per-CPU threshold.",
            "system.remote.load",
            profile_service,
            remote_runtime.check_load,
            _load_metrics,
        ),
        _runtime_tool(
            "get_remote_top_processes",
            "Read a minimal snapshot of the top remote processes by CPU usage.",
            "system.remote.processes",
            profile_service,
            remote_runtime.get_top_processes,
            _process_metrics,
        ),
        _runtime_tool(
            "check_remote_failed_services",
            "Check for failed remote system services without reading logs.",
            "system.remote.services",
            profile_service,
            remote_runtime.check_failed_services,
            _service_metrics,
        ),
    )


def _runtime_tool(
    name: str,
    description: str,
    evidence_id: str,
    profile_service: ProfileService,
    inspect: Callable[[Profile], RemoteLoadReport | RemoteProcessSnapshot | RemoteServiceReport],
    metrics: Callable[[object], dict[str, object]],
) -> Tool:
    def handler(arguments: BaseModel) -> ToolResult:
        if not isinstance(arguments, ProfileNameArguments):
            raise TypeError("unexpected tool argument model")
        report = inspect(profile_service.get(arguments.profile_name))
        evidence = build_diagnostic_evidence(evidence_id, "system", report.check)
        return ToolResult(
            name,
            True,
            {
                "evidence": [evidence.to_dict()],
                "metrics": metrics(report),
            },
        )

    return Tool(
        profile_scoped_tool_definition(name, description, category="system"),
        ProfileNameArguments,
        handler,
    )


def _load_metrics(report: object) -> dict[str, object]:
    if not isinstance(report, RemoteLoadReport) or report.load_1m is None:
        return {}
    return {
        "load_1m": report.load_1m,
        "load_5m": report.load_5m,
        "load_15m": report.load_15m,
        "cpu_count": report.cpu_count,
        "load_1m_per_cpu": report.load_1m_per_cpu,
    }


def _process_metrics(report: object) -> dict[str, object]:
    if not isinstance(report, RemoteProcessSnapshot):
        raise TypeError("unexpected process snapshot")
    return {"processes": [entry.to_dict() for entry in report.processes]}


def _service_metrics(report: object) -> dict[str, object]:
    if not isinstance(report, RemoteServiceReport):
        raise TypeError("unexpected service report")
    return {
        "failed_services": list(report.failed_services),
        "truncated": report.truncated,
    }
