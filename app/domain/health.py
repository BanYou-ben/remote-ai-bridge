from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


class BridgeHealth(str, Enum):
    DISCONNECTED = "Disconnected"
    DEGRADED = "Degraded"
    READY = "Ready"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str
    error_code: str | None = None
    http_status: int | None = None

    @property
    def passed(self) -> bool:
        return self.status is CheckStatus.PASS


@dataclass(frozen=True)
class HealthReport:
    local_tcp: CheckResult
    proxy_handshake: CheckResult
    tunnel_process: CheckResult
    remote_listener: CheckResult
    endpoint_probe: CheckResult

    @property
    def health(self) -> BridgeHealth:
        checks = (
            self.local_tcp,
            self.proxy_handshake,
            self.tunnel_process,
            self.remote_listener,
            self.endpoint_probe,
        )
        if all(check.passed for check in checks):
            return BridgeHealth.READY
        if self.tunnel_process.passed:
            return BridgeHealth.DEGRADED
        return BridgeHealth.DISCONNECTED

