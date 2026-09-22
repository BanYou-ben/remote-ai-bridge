from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from app.redaction import redact


class SupervisorState(str, Enum):
    STARTING = "STARTING"
    CONNECTING = "CONNECTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    UNSUPERVISED = "UNSUPERVISED"


@dataclass(frozen=True)
class SupervisorSnapshot:
    profile_name: str
    state: SupervisorState
    updated_at: str
    attempt: int = 0
    error_code: str | None = None
    message: str = ""
    retry_in_seconds: float | None = None
    pid: int | None = None
    tunnel_id: str | None = None
    remote_port: int | None = None
    last_successful_probe_at: str | None = None
    suggested_port: int | None = None
    supervised: bool = True
    runtime_present: bool = False
    process_alive: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", redact(self.message))

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_name": self.profile_name,
            "state": self.state.value,
            "updated_at": self.updated_at,
            "attempt": self.attempt,
            "error_code": self.error_code,
            "message": self.message,
            "retry_in_seconds": self.retry_in_seconds,
            "pid": self.pid,
            "tunnel_id": self.tunnel_id,
            "remote_port": self.remote_port,
            "last_successful_probe_at": self.last_successful_probe_at,
            "suggested_port": self.suggested_port,
            "supervised": self.supervised,
            "runtime_present": self.runtime_present,
            "process_alive": self.process_alive,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
