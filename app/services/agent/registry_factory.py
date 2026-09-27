from __future__ import annotations

from app.services.agent.connection_tools import build_connection_tools
from app.services.agent.network_tools import build_network_tools
from app.services.agent.runtime_tools import build_runtime_tools
from app.services.agent.system_tools import build_system_tools
from app.services.agent.tool_registry import ToolRegistry
from app.services.doctor import DoctorService
from app.services.local_proxy import LocalProxyService
from app.services.network_transport import NetworkTransportService
from app.services.profile_service import ProfileService
from app.services.remote_probe import RemoteProbeService
from app.services.remote_runtime import RemoteRuntimeService
from app.services.remote_system import RemoteSystemService
from app.services.runtime_manager import RuntimeManager
from app.services.ssh_config import SSHConfigService
from app.services.tunnel import TunnelManager


def build_agent_tool_registry(
    *,
    profiles: ProfileService,
    runtime_manager: RuntimeManager,
    doctor: DoctorService,
    local_proxy: LocalProxyService,
    ssh_config: SSHConfigService,
    tunnel_manager: TunnelManager,
    remote_probe: RemoteProbeService,
    network_transport: NetworkTransportService,
    remote_system: RemoteSystemService,
    remote_runtime: RemoteRuntimeService,
) -> ToolRegistry:
    """Create the complete deterministic read-only Agent tool catalog."""
    registry = ToolRegistry()
    tools = (
        *build_connection_tools(runtime_manager, profiles, doctor),
        *build_network_tools(
            profiles,
            local_proxy,
            ssh_config,
            network_transport,
            tunnel_manager,
            remote_probe,
        ),
        *build_system_tools(profiles, remote_system),
        *build_runtime_tools(profiles, remote_runtime),
    )
    for tool in tools:
        registry.register(tool)
    return registry
