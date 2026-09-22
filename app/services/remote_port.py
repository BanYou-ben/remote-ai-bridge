from __future__ import annotations

from app.domain.errors import RABError
from app.domain.profile import Profile
from app.services.remote_probe import RemoteProbeService


DEFAULT_REMOTE_PORT = 17890
DEFAULT_MAX_ATTEMPTS = 20
REMOTE_BIND_HOST = "127.0.0.1"


class RemotePortSelector:
    """Select a loopback port only during first-time managed profile setup."""

    def __init__(self, remote_probe: RemoteProbeService) -> None:
        self.remote_probe = remote_probe

    def select(
        self,
        profile: Profile,
        *,
        start_port: int = DEFAULT_REMOTE_PORT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> int:
        if profile.schema_version != 2 or profile.profile_type != "managed":
            raise RABError(
                "REMOTE_PORT_SELECTION_INVALID",
                "remote port discovery is restricted to new managed profiles",
            )
        if profile.remote_bind_host != REMOTE_BIND_HOST:
            raise RABError(
                "UNSAFE_REMOTE_BINDING",
                "remote port discovery is restricted to 127.0.0.1",
            )
        self.validate_scan_parameters(start_port=start_port, max_attempts=max_attempts)

        attempt_count = min(max_attempts, 65536 - start_port)
        for candidate in range(start_port, start_port + attempt_count):
            result = self.remote_probe.check_listener(profile, candidate)
            if result.error_code == "LISTENER_ABSENT":
                return candidate
            if result.passed or result.error_code == "UNSAFE_REMOTE_BINDING":
                continue
            raise RABError(
                "REMOTE_PORT_CHECK_FAILED",
                "remote port availability could not be checked",
                retryable=True,
                details={
                    "remote_bind_host": REMOTE_BIND_HOST,
                    "port": candidate,
                    "remote_error": result.error_code or "UNKNOWN",
                },
            )
        raise RABError(
            "REMOTE_PORT_RANGE_EXHAUSTED",
            "no free remote loopback port was found in the bounded scan range",
            retryable=True,
            details={"start_port": start_port, "max_attempts": max_attempts, "attempt_count": attempt_count},
        )

    @staticmethod
    def validate_scan_parameters(*, start_port: int, max_attempts: int) -> None:
        if isinstance(start_port, bool) or not isinstance(start_port, int) or not 1 <= start_port <= 65535:
            raise RABError("REMOTE_PORT_SELECTION_INVALID", "remote port scan start is invalid")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
            raise RABError("REMOTE_PORT_SELECTION_INVALID", "remote port scan limit is invalid")
