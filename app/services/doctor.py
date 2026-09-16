from __future__ import annotations

from dataclasses import dataclass

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.services.local_proxy import LocalProxyReport, LocalProxyService
from app.services.remote_probe import RemoteProbeService
from app.services.ssh_config import SSHConfigService
from app.services.tunnel import TunnelManager


@dataclass(frozen=True)
class DoctorReport:
    local: LocalProxyReport
    ssh: CheckResult
    tunnel: CheckResult
    remote_listener: CheckResult
    remote_endpoint: CheckResult


class DoctorService:
    def __init__(
        self,
        local_proxy: LocalProxyService,
        ssh_config: SSHConfigService,
        tunnel: TunnelManager,
        remote_probe: RemoteProbeService,
    ) -> None:
        self.local_proxy = local_proxy
        self.ssh_config = ssh_config
        self.tunnel = tunnel
        self.remote_probe = remote_probe

    def run(self, profile: Profile) -> DoctorReport:
        local = self.local_proxy.inspect(profile)
        ssh = self.ssh_config.check(profile.ssh_target)
        tunnel = self.tunnel.process_check(profile.name)
        if tunnel.passed and ssh.passed:
            listener = self.remote_probe.check_listener(profile)
            endpoint = (
                self.remote_probe.check_endpoint(profile)
                if listener.passed
                else CheckResult("Remote endpoint probe", CheckStatus.SKIP, "remote listener unavailable")
            )
        else:
            listener = CheckResult("Remote listener", CheckStatus.SKIP, "active owned tunnel unavailable")
            endpoint = CheckResult("Remote endpoint probe", CheckStatus.SKIP, "active owned tunnel unavailable")
        return DoctorReport(local, ssh, tunnel, listener, endpoint)
