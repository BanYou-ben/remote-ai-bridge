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
from app.services.ai.context_builder import (
    build_diagnostic_evidence,
    build_local_proxy_evidence,
)
from app.services.ai.schemas import DiagnosticEvidence
from app.services.local_proxy import LocalProxyService
from app.services.network_transport import NetworkTransportService
from app.services.profile_service import ProfileService
from app.services.remote_probe import RemoteProbeService
from app.services.ssh_config import SSHConfigService
from app.services.tunnel import TunnelManager


def build_network_tools(
    profile_service: ProfileService,
    local_proxy: LocalProxyService,
    ssh_config: SSHConfigService,
    network_transport: NetworkTransportService,
    tunnel: TunnelManager,
    remote_probe: RemoteProbeService,
) -> tuple[Tool, ...]:
    return (
        _profile_tool(
            "check_local_proxy",
            "Check the configured local proxy path without modifying it.",
            "connection",
            profile_service,
            lambda profile: build_local_proxy_evidence(local_proxy.inspect(profile)),
        ),
        _profile_tool(
            "check_ssh_config",
            "Check the trusted SSH configuration for the profile.",
            "connection",
            profile_service,
            lambda profile: (
                build_diagnostic_evidence(
                    "connection.ssh.config",
                    "connection",
                    ssh_config.check(profile),
                ),
            ),
        ),
        _profile_tool(
            "check_ssh_transport",
            "Check DNS and TCP reachability of the profile SSH endpoint.",
            "network",
            profile_service,
            lambda profile: _transport_evidence(network_transport, profile),
        ),
        _profile_tool(
            "check_tunnel_process",
            "Check the owned tunnel process state for the profile.",
            "connection",
            profile_service,
            lambda profile: (
                build_diagnostic_evidence(
                    "connection.tunnel.process",
                    "connection",
                    tunnel.process_check(profile.name),
                ),
            ),
        ),
        _profile_tool(
            "check_remote_listener",
            "Check the profile remote loopback listener.",
            "connection",
            profile_service,
            lambda profile: (
                build_diagnostic_evidence(
                    "connection.remote.listener",
                    "connection",
                    remote_probe.check_listener(profile),
                ),
            ),
        ),
        _profile_tool(
            "check_remote_endpoint",
            "Check endpoint connectivity through the profile bridge.",
            "connection",
            profile_service,
            lambda profile: (
                build_diagnostic_evidence(
                    "connection.remote.endpoint",
                    "connection",
                    remote_probe.check_endpoint(profile),
                ),
            ),
        ),
    )


def _profile_tool(
    name: str,
    description: str,
    category: str,
    profile_service: ProfileService,
    inspect: Callable[[Profile], tuple[DiagnosticEvidence, ...]],
) -> Tool:
    def handler(arguments: BaseModel) -> ToolResult:
        if not isinstance(arguments, ProfileNameArguments):
            raise TypeError("unexpected tool argument model")
        profile = profile_service.get(arguments.profile_name)
        evidence = inspect(profile)
        return ToolResult(
            name,
            True,
            {"evidence": [item.to_dict() for item in evidence]},
        )

    return Tool(
        profile_scoped_tool_definition(
            name,
            description,
            category=category,
        ),
        ProfileNameArguments,
        handler,
    )


def _transport_evidence(
    service: NetworkTransportService,
    profile: Profile,
) -> tuple[DiagnosticEvidence, ...]:
    report = service.check_ssh_transport(profile)
    return (
        build_diagnostic_evidence("network.ssh.dns", "network", report.dns),
        build_diagnostic_evidence("network.ssh.tcp", "network", report.tcp),
    )
