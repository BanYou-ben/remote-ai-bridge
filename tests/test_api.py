from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.domain.errors import RABError
from app.domain.supervisor import SupervisorSnapshot, SupervisorState


NOW = "2026-09-22T08:00:00+00:00"


def snapshot(
    name: str = "server-a",
    state: SupervisorState = SupervisorState.STARTING,
    *,
    error_code: str | None = None,
    message: str = "starting",
) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        profile_name=name,
        state=state,
        updated_at=NOW,
        attempt=2,
        error_code=error_code,
        message=message,
        retry_in_seconds=1.5,
        pid=123,
        tunnel_id="tunnel-1",
        remote_port=17890,
        last_successful_probe_at=None,
        suggested_port=None,
        supervised=True,
        runtime_present=True,
        process_alive=True,
    )


def services(*, shutdown_completed: bool = True):
    runtime = MagicMock()
    runtime.shutdown.return_value = SimpleNamespace(completed=shutdown_completed, results=())
    return SimpleNamespace(runtime_manager=runtime), runtime


def test_health_is_process_liveness_only():
    selected, runtime = services()

    with TestClient(create_app(services=selected)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "remote-ai-bridge"}
    runtime.list_status.assert_not_called()
    runtime.status.assert_not_called()
    runtime.start.assert_not_called()
    runtime.stop.assert_not_called()


def test_runtime_list_delegates_without_additional_work():
    selected, runtime = services()
    runtime.list_status.return_value = (snapshot(), snapshot("server-b", SupervisorState.READY))

    with TestClient(create_app(services=selected)) as client:
        response = client.get("/runtime")

    assert response.status_code == 200
    assert [item["profile_name"] for item in response.json()] == ["server-a", "server-b"]
    runtime.list_status.assert_called_once_with()


def test_runtime_status_delegates_by_name():
    selected, runtime = services()
    runtime.status.return_value = snapshot(state=SupervisorState.READY)

    with TestClient(create_app(services=selected)) as client:
        response = client.get("/runtime/server-a")

    assert response.status_code == 200
    runtime.status.assert_called_once_with("server-a")


def test_connect_calls_start_once_and_does_not_wait_for_ready():
    selected, runtime = services()
    runtime.start.return_value = snapshot(state=SupervisorState.CONNECTING)

    with TestClient(create_app(services=selected)) as client:
        response = client.post("/runtime/server-a/connect")

    assert response.status_code == 200
    assert response.json()["state"] == "CONNECTING"
    runtime.start.assert_called_once_with("server-a")
    runtime.wait.assert_not_called()


def test_duplicate_connect_preserves_runtime_manager_idempotence_boundary():
    selected, runtime = services()
    current = snapshot(state=SupervisorState.READY)
    runtime.start.return_value = current

    with TestClient(create_app(services=selected)) as client:
        first = client.post("/runtime/server-a/connect")
        second = client.post("/runtime/server-a/connect")

    assert first.json() == second.json()
    assert runtime.start.call_args_list == [(('server-a',),), (('server-a',),)]
    runtime.wait.assert_not_called()


def test_disconnect_delegates_only_to_runtime_manager_stop():
    selected, runtime = services()
    runtime.stop.return_value = SimpleNamespace(snapshot=snapshot(state=SupervisorState.STOPPED))

    with TestClient(create_app(services=selected)) as client:
        response = client.post("/runtime/server-a/disconnect")

    assert response.status_code == 200
    assert response.json()["state"] == "STOPPED"
    runtime.stop.assert_called_once_with("server-a")


def test_runtime_status_rejects_invalid_profile_name_before_runtime_call():
    selected, runtime = services()

    with TestClient(create_app(services=selected)) as client:
        response = client.get("/runtime/bad:name")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROFILE_INVALID"
    assert response.json()["error"]["retryable"] is False
    runtime.status.assert_not_called()


def test_connect_rejects_invalid_profile_name_before_runtime_call():
    selected, runtime = services()

    with TestClient(create_app(services=selected)) as client:
        response = client.post("/runtime/bad:name/connect")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROFILE_INVALID"
    runtime.start.assert_not_called()


def test_disconnect_rejects_invalid_profile_name_before_runtime_call():
    selected, runtime = services()

    with TestClient(create_app(services=selected)) as client:
        response = client.post("/runtime/bad:name/disconnect")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROFILE_INVALID"
    runtime.stop.assert_not_called()


def test_disconnect_profile_busy_returns_stable_409_error():
    selected, runtime = services()
    runtime.stop.return_value = SimpleNamespace(
        snapshot=snapshot(
            state=SupervisorState.FAILED,
            error_code="PROFILE_BUSY",
            message="profile is busy",
        )
    )

    with TestClient(create_app(services=selected)) as client:
        response = client.post("/runtime/server-a/disconnect")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "PROFILE_BUSY",
            "message": "profile is busy",
            "retryable": True,
            "details": {"name": "server-a"},
        }
    }


def test_disconnect_identity_mismatch_returns_non_retryable_409():
    selected, runtime = services()
    runtime.stop.return_value = SimpleNamespace(
        snapshot=snapshot(
            state=SupervisorState.FAILED,
            error_code="PROCESS_IDENTITY_MISMATCH",
            message="identity mismatch; runtime evidence retained",
        )
    )

    with TestClient(create_app(services=selected)) as client:
        response = client.post("/runtime/server-a/disconnect")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PROCESS_IDENTITY_MISMATCH"
    assert response.json()["error"]["retryable"] is False


def test_disconnect_stop_timeout_returns_retryable_503():
    selected, runtime = services()
    runtime.stop.return_value = SimpleNamespace(
        snapshot=snapshot(
            state=SupervisorState.FAILED,
            error_code="PROCESS_STOP_TIMEOUT",
            message="owned process did not stop before timeout",
        )
    )

    with TestClient(create_app(services=selected)) as client:
        response = client.post("/runtime/server-a/disconnect")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "PROCESS_STOP_TIMEOUT"
    assert response.json()["error"]["retryable"] is True


def test_disconnect_unknown_failure_returns_non_retryable_500():
    selected, runtime = services()
    runtime.stop.return_value = SimpleNamespace(
        snapshot=snapshot(
            state=SupervisorState.FAILED,
            error_code="UNEXPECTED_STOP_FAILURE",
            message="stop failed",
        )
    )

    with TestClient(create_app(services=selected)) as client:
        response = client.post("/runtime/server-a/disconnect")

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "RUNTIME_STOP_FAILED",
        "message": "stop failed",
        "retryable": False,
        "details": {"name": "server-a", "stop_error_code": "UNEXPECTED_STOP_FAILURE"},
    }


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("PROFILE_NOT_FOUND", 404),
        ("PROFILE_BUSY", 409),
        ("RUNTIME_STOP_IN_PROGRESS", 409),
        ("RUNTIME_MANAGER_SHUTTING_DOWN", 503),
        ("PROFILE_INVALID", 422),
        ("SOME_KNOWN_RAB_ERROR", 400),
    ],
)
def test_rab_error_has_stable_json_and_status_mapping(code, expected_status):
    selected, runtime = services()
    runtime.status.side_effect = RABError(
        code,
        "request failed",
        retryable=True,
        details={"name": "server-a"},
    )

    with TestClient(create_app(services=selected)) as client:
        response = client.get("/runtime/server-a")

    assert response.status_code == expected_status
    assert response.json() == {
        "error": {
            "code": code,
            "message": "request failed",
            "retryable": True,
            "details": {"name": "server-a"},
        }
    }


def test_rab_error_details_remain_redacted():
    selected, runtime = services()
    runtime.status.side_effect = RABError(
        "PROFILE_BUSY",
        "Authorization: Bearer secret-value",
        details={"accessToken": "secret-value"},
    )

    with TestClient(create_app(services=selected)) as client:
        response = client.get("/runtime/server-a")

    body = response.json()
    assert "secret-value" not in response.text
    assert body["error"]["details"]["accessToken"] == "[REDACTED]"


def test_unexpected_exception_returns_generic_500_without_secret():
    selected, runtime = services()
    runtime.status.side_effect = RuntimeError("password=secret-value")

    with TestClient(create_app(services=selected), raise_server_exceptions=False) as client:
        response = client.get("/runtime/server-a")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "internal server error",
            "retryable": False,
            "details": {},
        }
    }
    assert "secret-value" not in response.text


def test_snapshot_response_has_stable_enum_time_and_optional_fields():
    selected, runtime = services()
    runtime.status.return_value = snapshot(state=SupervisorState.DEGRADED)

    with TestClient(create_app(services=selected)) as client:
        body = client.get("/runtime/server-a").json()

    assert body == {
        "profile_name": "server-a",
        "state": "DEGRADED",
        "updated_at": NOW,
        "attempt": 2,
        "error_code": None,
        "message": "starting",
        "retry_in_seconds": 1.5,
        "pid": 123,
        "tunnel_id": "tunnel-1",
        "remote_port": 17890,
        "last_successful_probe_at": None,
        "suggested_port": None,
        "supervised": True,
        "runtime_present": True,
        "process_alive": True,
    }


def test_lifespan_uses_injected_runtime_and_shuts_it_down_once():
    selected, runtime = services()
    runtime.status.return_value = snapshot()
    application = create_app(services=selected)

    with TestClient(application) as client:
        assert application.state.services.runtime_manager is runtime
        client.get("/runtime/server-a")

    runtime.shutdown.assert_called_once_with()


def test_lifespan_constructs_services_once_at_startup():
    selected, runtime = services()
    runtime.status.return_value = snapshot()
    with patch("app.api.app.create_services", return_value=selected) as factory:
        application = create_app(state_dir=None)
        factory.assert_not_called()
        with TestClient(application) as client:
            client.get("/runtime/server-a")
            client.get("/runtime/server-a")

    factory.assert_called_once_with(None)
    runtime.shutdown.assert_called_once_with()


def test_incomplete_shutdown_is_recorded_without_blind_cleanup(caplog):
    selected, runtime = services(shutdown_completed=False)
    application = create_app(services=selected)

    with TestClient(application):
        pass

    runtime.shutdown.assert_called_once_with()
    assert application.state.runtime_shutdown_result.completed is False
    assert "runtime manager shutdown incomplete" in caplog.text
    runtime.stop.assert_not_called()
