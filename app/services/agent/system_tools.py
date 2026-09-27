from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

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
from app.services.remote_system import (
    RemoteDiskReport,
    RemoteMemoryReport,
    RemoteSystemInfo,
    RemoteSystemService,
)


def build_system_tools(
    profile_service: ProfileService,
    remote_system: RemoteSystemService,
) -> tuple[Tool, ...]:
    return (
        _system_tool(
            "get_remote_system_info",
            "Read safe operating system metadata from the profile remote host.",
            "system.remote.os",
            profile_service,
            remote_system.get_system_info,
            _system_metrics,
        ),
        _system_tool(
            "check_remote_memory",
            "Check remote available memory against the configured threshold.",
            "system.remote.memory",
            profile_service,
            remote_system.check_memory,
            _memory_metrics,
        ),
        _system_tool(
            "check_remote_disk",
            "Check remote root filesystem usage against the configured threshold.",
            "system.remote.disk",
            profile_service,
            remote_system.check_disk,
            _disk_metrics,
        ),
    )


def _system_tool(
    name: str,
    description: str,
    evidence_id: str,
    profile_service: ProfileService,
    inspect: Callable[[Profile], RemoteSystemInfo | RemoteMemoryReport | RemoteDiskReport],
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


def _system_metrics(report: object) -> dict[str, object]:
    if not isinstance(report, RemoteSystemInfo):
        raise TypeError("unexpected system information report")
    return _present_fields(report, "os_name", "os_version", "kernel", "architecture")


def _memory_metrics(report: object) -> dict[str, object]:
    if not isinstance(report, RemoteMemoryReport):
        raise TypeError("unexpected memory report")
    return _present_fields(report, "total_bytes", "available_bytes", "available_percent")


def _disk_metrics(report: object) -> dict[str, object]:
    if not isinstance(report, RemoteDiskReport):
        raise TypeError("unexpected disk report")
    return _present_fields(
        report,
        "total_bytes",
        "used_bytes",
        "available_bytes",
        "used_percent",
    )


def _present_fields(report: object, *names: str) -> dict[str, object]:
    values = asdict(report)
    return {name: values[name] for name in names if values[name] is not None}
