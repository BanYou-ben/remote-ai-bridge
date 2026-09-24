from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr, StrictBool, StrictInt, StrictStr

from app.domain.health import CheckResult
from app.domain.profile import Profile
from app.domain.supervisor import SupervisorSnapshot, SupervisorState
from app.services.doctor import DoctorReport
from app.services.local_proxy import LocalProxyCandidate, LocalProxyDiscovery
from app.services.profile_service import ProfileDeleteResult
from app.domain.ssh_bootstrap import HostPreparation
from app.redaction import redact


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class ProfileResponse(BaseModel):
    schema_version: int
    name: str
    ssh_target: str
    local_proxy_host: str
    local_proxy_port: int
    remote_bind_host: str
    remote_port: int
    auto_reconnect: bool
    endpoint_probe_url: str
    profile_type: str
    host: str | None
    username: str | None
    ssh_port: int | None
    key_id: str | None
    host_key_type: str | None
    host_key_fingerprint: str | None

    @classmethod
    def from_profile(cls, profile: Profile) -> "ProfileResponse":
        return cls(
            schema_version=profile.schema_version,
            name=profile.name,
            ssh_target=profile.ssh_target,
            local_proxy_host=profile.local_proxy_host,
            local_proxy_port=profile.local_proxy_port,
            remote_bind_host=profile.remote_bind_host,
            remote_port=profile.remote_port,
            auto_reconnect=profile.auto_reconnect,
            endpoint_probe_url=profile.endpoint_probe_url,
            profile_type=profile.profile_type,
            host=profile.host,
            username=profile.username,
            ssh_port=profile.ssh_port,
            key_id=profile.key_id,
            host_key_type=profile.host_key_type,
            host_key_fingerprint=profile.host_key_fingerprint,
        )


class ProfileUpdateRequest(StrictRequest):
    ssh_target: StrictStr | None = None
    local_proxy_port: StrictInt | None = None
    remote_port: StrictInt | None = None
    auto_reconnect: StrictBool | None = None
    endpoint_probe_url: StrictStr | None = None


class ProfileDeleteResponse(BaseModel):
    name: str
    stopped_owned_process: bool
    removed_stale_runtime: bool

    @classmethod
    def from_result(cls, result: ProfileDeleteResult) -> "ProfileDeleteResponse":
        return cls(
            name=result.name,
            stopped_owned_process=result.stopped_owned_process,
            removed_stale_runtime=result.removed_stale_runtime,
        )


class HostPrepareRequest(StrictRequest):
    host: StrictStr
    username: StrictStr
    port: StrictInt = 22


class HostConfirmRequest(StrictRequest):
    host: StrictStr
    port: StrictInt = 22
    expected_fingerprint: StrictStr
    accepted: StrictBool


class HostPreparationResponse(BaseModel):
    host: str
    port: int
    key_type: str
    fingerprint: str
    status: str

    @classmethod
    def from_preparation(cls, value: HostPreparation) -> "HostPreparationResponse":
        return cls(
            host=value.host,
            port=value.port,
            key_type=value.key_type,
            fingerprint=value.fingerprint,
            status=value.status,
        )


class LocalProxyDiscoverRequest(StrictRequest):
    endpoint_probe_url: StrictStr = "https://api.openai.com/v1/models"
    candidate_ports: tuple[StrictInt, ...] | None = None
    selected_local_proxy_port: StrictInt | None = None


class LocalProxyCandidateResponse(BaseModel):
    host: str
    port: int
    tcp_reachable: bool
    connect_reachable: bool
    endpoint_reachable: bool
    error_code: str | None

    @classmethod
    def from_candidate(cls, value: LocalProxyCandidate) -> "LocalProxyCandidateResponse":
        return cls(**value.to_dict())


class LocalProxyDiscoveryResponse(BaseModel):
    selected: LocalProxyCandidateResponse
    candidates: list[LocalProxyCandidateResponse]

    @classmethod
    def from_discovery(cls, value: LocalProxyDiscovery) -> "LocalProxyDiscoveryResponse":
        return cls(
            selected=LocalProxyCandidateResponse.from_candidate(value.selected),
            candidates=[LocalProxyCandidateResponse.from_candidate(item) for item in value.candidates],
        )


class ManagedSetupRequest(StrictRequest):
    name: StrictStr
    host: StrictStr
    username: StrictStr
    password: SecretStr
    port: StrictInt = 22
    confirmed_fingerprint: StrictStr | None = None
    selected_local_proxy_port: StrictInt | None = None
    candidate_proxy_ports: tuple[StrictInt, ...] | None = None
    start_remote_port: StrictInt = 17890
    max_remote_port_attempts: StrictInt = 20
    auto_reconnect: StrictBool = True
    endpoint_probe_url: StrictStr = "https://api.openai.com/v1/models"


class CheckResultResponse(BaseModel):
    name: str
    status: str
    detail: str
    error_code: str | None
    http_status: int | None

    @classmethod
    def from_check(cls, check: CheckResult) -> "CheckResultResponse":
        return cls(
            name=check.name,
            status=check.status.value,
            detail=redact(check.detail),
            error_code=check.error_code,
            http_status=check.http_status,
        )


class LocalProxyReportResponse(BaseModel):
    tcp: CheckResultResponse
    handshake: CheckResultResponse
    endpoint: CheckResultResponse


class DoctorReportResponse(BaseModel):
    local: LocalProxyReportResponse
    ssh: CheckResultResponse
    tunnel: CheckResultResponse
    remote_listener: CheckResultResponse
    remote_endpoint: CheckResultResponse

    @classmethod
    def from_report(cls, report: DoctorReport) -> "DoctorReportResponse":
        return cls(
            local=LocalProxyReportResponse(
                tcp=CheckResultResponse.from_check(report.local.tcp),
                handshake=CheckResultResponse.from_check(report.local.handshake),
                endpoint=CheckResultResponse.from_check(report.local.endpoint),
            ),
            ssh=CheckResultResponse.from_check(report.ssh),
            tunnel=CheckResultResponse.from_check(report.tunnel),
            remote_listener=CheckResultResponse.from_check(report.remote_listener),
            remote_endpoint=CheckResultResponse.from_check(report.remote_endpoint),
        )
