from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.infrastructure.process_identity import ProcessInspector
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.profile_store import ProfileStore
from app.services.doctor import DoctorService
from app.services.local_proxy import LocalProxyService
from app.services.profile_service import ProfileService
from app.services.remote_probe import RemoteProbeService
from app.services.runtime_manager import RuntimeManager
from app.services.ssh_config import SSHConfigService, locate_ssh
from app.services.tunnel import TunnelManager


@dataclass(frozen=True)
class AppServices:
    store: ProfileStore
    profiles: ProfileService
    local_proxy: LocalProxyService
    ssh_config: SSHConfigService
    remote_probe: RemoteProbeService
    tunnel_manager: TunnelManager
    doctor: DoctorService
    runtime_manager: RuntimeManager


def create_services(state_dir: Path | None = None) -> AppServices:
    store = ProfileStore(state_dir)
    runner = ProcessRunner()
    inspector = ProcessInspector()
    ssh_executable = locate_ssh()
    if ssh_executable is None:
        raise RuntimeError("Windows OpenSSH ssh.exe was not found on PATH")
    local_proxy = LocalProxyService()
    remote_probe = RemoteProbeService(runner, ssh_executable, state_root=store.root)
    ssh_config = SSHConfigService(runner, ssh_executable, state_root=store.root)
    tunnel_manager = TunnelManager(runner, inspector, store, remote_probe, ssh_executable)
    doctor = DoctorService(local_proxy, ssh_config, tunnel_manager, remote_probe)
    profiles = ProfileService(store, tunnel_manager)
    runtime_manager = RuntimeManager(profiles, store, local_proxy, tunnel_manager)
    return AppServices(
        store=store,
        profiles=profiles,
        local_proxy=local_proxy,
        ssh_config=ssh_config,
        remote_probe=remote_probe,
        tunnel_manager=tunnel_manager,
        doctor=doctor,
        runtime_manager=runtime_manager,
    )
