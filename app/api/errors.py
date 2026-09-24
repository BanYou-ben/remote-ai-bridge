from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.errors import RABError
from app.domain.profile import ProfileValidationError, validate_profile_name
from app.domain.supervisor import SupervisorSnapshot, SupervisorState
from app.redaction import redact


HTTP_STATUS_BY_ERROR_CODE = {
    "PROFILE_NOT_FOUND": 404,
    "PROFILE_EXISTS": 409,
    "PROFILE_BUSY": 409,
    "RUNTIME_STOP_IN_PROGRESS": 409,
    "PROCESS_IDENTITY_MISMATCH": 409,
    "PROCESS_STOP_TIMEOUT": 503,
    "RUNTIME_STOP_FAILED": 500,
    "REMOTE_PORT_CONFLICT": 409,
    "PROFILE_INVALID": 422,
    "PROFILE_UPDATE_EMPTY": 422,
    "PROFILE_FIELD_NOT_UPDATABLE": 422,
    "PROFILE_RUNTIME_ACTIVE": 409,
    "MANAGED_PROFILE_CREDENTIAL_CLEANUP_REQUIRED": 409,
    "PROFILE_DELETE_REMOTE_STATE_UNRESOLVED": 409,
    "PROFILE_DELETE_RUNTIME_REMAINS": 409,
    "PROFILE_DELETE_TUNNEL_STOP_FAILED": 503,
    "HOST_KEY_CONFIRMATION_REQUIRED": 409,
    "HOST_KEY_CHANGED": 409,
    "LOCAL_PROXY_SELECTION_REQUIRED": 409,
    "MANAGED_SETUP_UNAVAILABLE": 503,
    "SETUP_ROLLBACK_FAILED": 500,
    "SSH_BOOTSTRAP_FAILED": 503,
    "RUNTIME_MANAGER_SHUTTING_DOWN": 503,
}

RETRYABLE_STOP_FAILURES = frozenset(
    {
        "PROFILE_BUSY",
        "RUNTIME_STOP_IN_PROGRESS",
        "PROCESS_STOP_TIMEOUT",
    }
)
PUBLIC_STOP_FAILURES = frozenset(
    {
        "PROFILE_BUSY",
        "RUNTIME_STOP_IN_PROGRESS",
        "PROCESS_IDENTITY_MISMATCH",
        "PROCESS_STOP_TIMEOUT",
    }
)


def validated_profile_name(name: str) -> str:
    try:
        validate_profile_name(name)
    except ProfileValidationError as exc:
        raise RABError(
            "PROFILE_INVALID",
            str(exc),
            details={"name": name},
        ) from exc
    return name


def stop_failure_error(snapshot: SupervisorSnapshot) -> RABError:
    if snapshot.state is not SupervisorState.FAILED:
        raise ValueError("stop failure adapter requires a FAILED supervisor snapshot")
    original_code = snapshot.error_code or "RUNTIME_STOP_FAILED"
    public_code = original_code if original_code in PUBLIC_STOP_FAILURES else "RUNTIME_STOP_FAILED"
    details: dict[str, object] = {"name": snapshot.profile_name}
    if public_code != original_code:
        details["stop_error_code"] = original_code
    return RABError(
        public_code,
        snapshot.message or "runtime stop failed",
        retryable=public_code in RETRYABLE_STOP_FAILURES,
        details=details,
    )


async def rab_error_handler(_request: Request, exc: RABError) -> JSONResponse:
    status_code = HTTP_STATUS_BY_ERROR_CODE.get(exc.code, 400)
    return JSONResponse(status_code=status_code, content={"error": exc.to_dict()})


async def request_validation_error_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    errors = [
        {
            "loc": list(item.get("loc", ())),
            "msg": redact(str(item.get("msg", "invalid value"))),
            "type": str(item.get("type", "value_error")),
        }
        for item in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "REQUEST_INVALID",
                "message": "request validation failed",
                "retryable": False,
                "details": {"errors": errors},
            }
        },
    )


def internal_server_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "internal server error",
                "retryable": False,
                "details": {},
            }
        },
    )
