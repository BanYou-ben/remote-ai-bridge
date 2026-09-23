from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.errors import stop_failure_error, validated_profile_name
from app.api.schemas import HealthResponse, RuntimeSnapshotResponse
from app.domain.supervisor import SupervisorState
from app.services.runtime_manager import RuntimeManager


router = APIRouter()


def _runtime(request: Request) -> RuntimeManager:
    return request.app.state.services.runtime_manager


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
