from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.domain.supervisor import SupervisorSnapshot, SupervisorState


class RuntimeSnapshotResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    profile_name: str
    state: SupervisorState
    updated_at: str
    attempt: int
    error_code: str | None
    message: str
    retry_in_seconds: float | None
    pid: int | None
    tunnel_id: str | None
    remote_port: int | None
    last_successful_probe_at: str | None
    suggested_port: int | None
    supervised: bool
    runtime_present: bool
    process_alive: bool | None

    @classmethod
    def from_snapshot(cls, snapshot: SupervisorSnapshot) -> "RuntimeSnapshotResponse":
        return cls(**snapshot.to_dict())


class HealthResponse(BaseModel):
    status: str
    service: str


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, object]


class ErrorResponse(BaseModel):
    error: ErrorBody
