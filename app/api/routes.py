from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from urllib.parse import urlsplit
import ipaddress

from app.api.errors import stop_failure_error, validated_profile_name
from app.api.schemas import (
    AIDiagnosisRequest,
    DoctorReportResponse,
    HealthResponse,
    HostConfirmRequest,
    HostPreparationResponse,
    HostPrepareRequest,
    LocalProxyDiscoverRequest,
    LocalProxyDiscoveryResponse,
    ManagedSetupRequest,
    ProfileDeleteResponse,
    ProfileResponse,
    ProfileUpdateRequest,
    RuntimeSnapshotResponse,
)
from app.bootstrap import AppServices
from app.domain.supervisor import SupervisorState
from app.domain.errors import RABError
from app.services.agent.web_diagnostics import DIAGNOSIS_GATE, create_deepseek_model, public_result, run_web_diagnostics, safe_question
from app.services.local_proxy import DEFAULT_CANDIDATE_PORTS
from app.services.runtime_manager import RuntimeManager


router = APIRouter()


def _runtime(request: Request) -> RuntimeManager:
    return _services(request).runtime_manager


def _services(request: Request) -> AppServices:
    return request.app.state.services


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service="remote-ai-bridge")


@router.get("/runtime", response_model=list[RuntimeSnapshotResponse])
def list_runtime(request: Request) -> list[RuntimeSnapshotResponse]:
    return [RuntimeSnapshotResponse.from_snapshot(item) for item in _runtime(request).list_status()]


@router.get("/runtime/{name}", response_model=RuntimeSnapshotResponse)
def runtime_status(name: str, request: Request) -> RuntimeSnapshotResponse:
    selected_name = validated_profile_name(name)
    return RuntimeSnapshotResponse.from_snapshot(_runtime(request).status(selected_name))


@router.post("/runtime/{name}/connect", response_model=RuntimeSnapshotResponse)
def connect_runtime(name: str, request: Request) -> RuntimeSnapshotResponse:
    selected_name = validated_profile_name(name)
    return RuntimeSnapshotResponse.from_snapshot(_runtime(request).start(selected_name))


@router.post("/runtime/{name}/disconnect", response_model=RuntimeSnapshotResponse)
def disconnect_runtime(name: str, request: Request) -> RuntimeSnapshotResponse:
    selected_name = validated_profile_name(name)
    snapshot = _runtime(request).stop(selected_name).snapshot
    if snapshot.state is SupervisorState.FAILED:
        raise stop_failure_error(snapshot)
    return RuntimeSnapshotResponse.from_snapshot(snapshot)


@router.get("/profiles", response_model=list[ProfileResponse])
def list_profiles(request: Request) -> list[ProfileResponse]:
    return [ProfileResponse.from_profile(item) for item in _services(request).profiles.list()]


@router.get("/profiles/{name}", response_model=ProfileResponse)
def get_profile(name: str, request: Request) -> ProfileResponse:
    selected_name = validated_profile_name(name)
    return ProfileResponse.from_profile(_services(request).profiles.get(selected_name))


@router.patch("/profiles/{name}", response_model=ProfileResponse)
def update_profile(name: str, payload: ProfileUpdateRequest, request: Request) -> ProfileResponse:
    selected_name = validated_profile_name(name)
    changes = payload.model_dump(exclude_unset=True)
    return ProfileResponse.from_profile(_services(request).profiles.update(selected_name, changes))


@router.delete("/profiles/{name}", response_model=ProfileDeleteResponse)
def delete_profile(name: str, request: Request) -> ProfileDeleteResponse:
    selected_name = validated_profile_name(name)
    return ProfileDeleteResponse.from_result(_services(request).profiles.delete(selected_name))


@router.post("/setup/host/prepare", response_model=HostPreparationResponse)
def prepare_host(payload: HostPrepareRequest, request: Request) -> HostPreparationResponse:
    result = _services(request).setup.prepare(payload.host, payload.username, payload.port)
    return HostPreparationResponse.from_preparation(result)


@router.post("/setup/host/confirm", response_model=HostPreparationResponse)
def confirm_host(payload: HostConfirmRequest, request: Request) -> HostPreparationResponse:
    result = _services(request).setup.confirm_host_key(
        payload.host,
        payload.port,
        payload.expected_fingerprint,
        accepted=payload.accepted,
    )
    return HostPreparationResponse.from_preparation(result)


@router.post("/setup/local-proxy/discover", response_model=LocalProxyDiscoveryResponse)
def discover_local_proxy(
    payload: LocalProxyDiscoverRequest,
    request: Request,
) -> LocalProxyDiscoveryResponse:
    candidate_ports = (
        DEFAULT_CANDIDATE_PORTS if payload.candidate_ports is None else payload.candidate_ports
    )
    result = _services(request).local_proxy.discover(
        payload.endpoint_probe_url,
        candidate_ports=candidate_ports,
        selected_port=payload.selected_local_proxy_port,
    )
    return LocalProxyDiscoveryResponse.from_discovery(result)


@router.post("/setup/managed", response_model=ProfileResponse)
def setup_managed_profile(payload: ManagedSetupRequest, request: Request) -> ProfileResponse:
    password = payload.password.get_secret_value()
    try:
        profile = _services(request).setup.setup_managed_profile(
            payload.name,
            payload.host,
            payload.username,
            password,
            port=payload.port,
            confirmed_fingerprint=payload.confirmed_fingerprint,
            selected_local_proxy_port=payload.selected_local_proxy_port,
            candidate_proxy_ports=(
                DEFAULT_CANDIDATE_PORTS
                if payload.candidate_proxy_ports is None
                else payload.candidate_proxy_ports
            ),
            start_remote_port=payload.start_remote_port,
            max_remote_port_attempts=payload.max_remote_port_attempts,
            auto_reconnect=payload.auto_reconnect,
            endpoint_probe_url=payload.endpoint_probe_url,
        )
        return ProfileResponse.from_profile(profile)
    finally:
        # This only drops this binding; immutable Python strings cannot be securely erased.
        password = ""


@router.post("/doctor/{name}", response_model=DoctorReportResponse)
def run_doctor(name: str, request: Request) -> DoctorReportResponse:
    selected_name = validated_profile_name(name)
    profile = _services(request).profiles.get(selected_name)
    return DoctorReportResponse.from_report(_services(request).doctor.run(profile))


def _loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@router.post("/ai/diagnose/{name}")
def ai_diagnose(name: str, payload: AIDiagnosisRequest, request: Request) -> JSONResponse:
    # Reject DNS-rebinding and remote origins before accepting credentials.
    host = urlsplit("//" + request.headers.get("host", "")).hostname
    origin_header = request.headers.get("origin")
    origin = urlsplit(origin_header) if origin_header else None
    if not _loopback_host(host) or (origin is not None and (origin.scheme not in {"http", "https"} or not _loopback_host(origin.hostname))):
        raise RABError("AI_ORIGIN_UNSAFE", "AI diagnostics requires a local origin")
    if not payload.consent:
        raise RABError("AI_CONSENT_REQUIRED", "explicit consent is required")
    selected_name = validated_profile_name(name)
    if not payload.question.strip() or len(payload.question) > 500:
        raise RABError("AI_QUESTION_INVALID", "question must contain 1 to 500 characters")
    secret = payload.api_key.get_secret_value()
    if not secret.strip() or len(secret) > 512:
        raise RABError("AI_KEY_INVALID", "API key is invalid")
    services = _services(request)
    services.profiles.get(selected_name)
    if not DIAGNOSIS_GATE.acquire(blocking=False):
        raise RABError("AI_DIAGNOSIS_BUSY", "an AI diagnosis is already running", retryable=True)
    try:
        model = create_deepseek_model(secret)
        result = run_web_diagnostics(services.agent_tool_registry, model, selected_name, safe_question(payload.question, secret), secret)
        return JSONResponse(content=public_result(result, secret), headers={"Cache-Control": "no-store"})
    finally:
        secret = ""
        DIAGNOSIS_GATE.release()
