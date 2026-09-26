from __future__ import annotations

from dataclasses import dataclass

from app.redaction import redact


@dataclass(frozen=True)
class DiagnosticProfileSummary:
    name: str
    profile_type: str
    local_proxy_host: str
    local_proxy_port: int
    remote_bind_host: str
    remote_port: int
    auto_reconnect: bool
    endpoint_probe_url: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "profile_type": self.profile_type,
            "local_proxy_host": self.local_proxy_host,
            "local_proxy_port": self.local_proxy_port,
            "remote_bind_host": self.remote_bind_host,
            "remote_port": self.remote_port,
            "auto_reconnect": self.auto_reconnect,
            "endpoint_probe_url": self.endpoint_probe_url,
        }


@dataclass(frozen=True)
class DiagnosticRuntimeSummary:
    state: str
    attempt: int
    error_code: str | None
    message: str
    retry_in_seconds: float | None
    remote_port: int | None
    last_successful_probe_at: str | None
    suggested_port: int | None
    supervised: bool
    runtime_present: bool
    process_alive: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "message", redact(self.message))

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "attempt": self.attempt,
            "error_code": self.error_code,
            "message": self.message,
            "retry_in_seconds": self.retry_in_seconds,
            "remote_port": self.remote_port,
            "last_successful_probe_at": self.last_successful_probe_at,
            "suggested_port": self.suggested_port,
            "supervised": self.supervised,
            "runtime_present": self.runtime_present,
            "process_alive": self.process_alive,
        }


@dataclass(frozen=True)
class DiagnosticEvidence:
    id: str
    category: str
    name: str
    status: str
    detail: str
    error_code: str | None
    http_status: int | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "detail", redact(self.detail))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "category": self.category,
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "error_code": self.error_code,
            "http_status": self.http_status,
        }


@dataclass(frozen=True)
class DiagnosticContext:
    profile: DiagnosticProfileSummary
    runtime: DiagnosticRuntimeSummary
    evidence: tuple[DiagnosticEvidence, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.to_dict(),
            "runtime": self.runtime.to_dict(),
            "evidence": [item.to_dict() for item in self.evidence],
        }
