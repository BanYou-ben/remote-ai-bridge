from __future__ import annotations

from dataclasses import dataclass
import socket

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile


@dataclass(frozen=True)
class NetworkTransportReport:
    dns: CheckResult
    tcp: CheckResult


class NetworkTransportService:
    def __init__(self, timeout: float = 5.0) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not 0 < timeout <= 60
        ):
            raise ValueError("network transport timeout must be greater than 0 and at most 60 seconds")
        self.timeout = float(timeout)

    def check_ssh_transport(self, profile: Profile) -> NetworkTransportReport:
        profile.validate()
        if profile.host is None or profile.ssh_port is None:
            unavailable = "managed SSH network target is unavailable for this profile"
            return NetworkTransportReport(
                CheckResult(
                    "SSH DNS resolution",
                    CheckStatus.SKIP,
                    unavailable,
                    "NETWORK_TARGET_UNAVAILABLE",
                ),
                CheckResult(
                    "SSH TCP transport",
                    CheckStatus.SKIP,
                    unavailable,
                    "NETWORK_TARGET_UNAVAILABLE",
                ),
            )

        try:
            socket.getaddrinfo(
                profile.host,
                profile.ssh_port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror:
            return NetworkTransportReport(
                CheckResult(
                    "SSH DNS resolution",
                    CheckStatus.FAIL,
                    "SSH host name resolution failed",
                    "SSH_DNS_RESOLUTION_FAILED",
                ),
                CheckResult(
                    "SSH TCP transport",
                    CheckStatus.SKIP,
                    "SSH TCP check skipped because DNS resolution failed",
                ),
            )

        dns = CheckResult(
            "SSH DNS resolution",
            CheckStatus.PASS,
            "SSH host resolved successfully",
        )
        try:
            with socket.create_connection(
                (profile.host, profile.ssh_port),
                timeout=self.timeout,
            ):
                pass
        except ConnectionRefusedError:
            tcp = CheckResult(
                "SSH TCP transport",
                CheckStatus.FAIL,
                "SSH TCP connection was refused",
                "SSH_TCP_REFUSED",
            )
        except TimeoutError:
            tcp = CheckResult(
                "SSH TCP transport",
                CheckStatus.FAIL,
                "SSH TCP connection timed out",
                "SSH_TCP_TIMEOUT",
            )
        except OSError:
            tcp = CheckResult(
                "SSH TCP transport",
                CheckStatus.FAIL,
                "SSH TCP endpoint is unreachable",
                "SSH_TCP_UNREACHABLE",
            )
        else:
            tcp = CheckResult(
                "SSH TCP transport",
                CheckStatus.PASS,
                "SSH TCP endpoint accepted a connection",
            )
        return NetworkTransportReport(dns, tcp)
