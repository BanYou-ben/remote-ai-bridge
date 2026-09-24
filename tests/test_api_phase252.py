from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.domain.errors import RABError
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.domain.ssh_bootstrap import HostPreparation
from app.services.doctor import DoctorReport
from app.services.local_proxy import LocalProxyCandidate, LocalProxyDiscovery, LocalProxyReport
from app.services.profile_service import ProfileDeleteResult


SECRET = "RAB-P25-SECRET-DO-NOT-LEAK"


def legacy_profile(name: str = "server-a") -> Profile:
    return Profile(1, name, name, local_proxy_port=7897, remote_port=17890)


def managed_profile(name: str = "managed-a") -> Profile:
    return Profile(
        schema_version=2,
        name=name,
        ssh_target="alice@example.test",
        local_proxy_port=7897,
        remote_port=17890,
        profile_type="managed",
        host="example.test",
        username="alice",
        ssh_port=22,
        key_id="a" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:" + "A" * 43,
    )


def api_services():
    runtime = MagicMock()
    runtime.shutdown.return_value = SimpleNamespace(completed=True, results=())
    selected = SimpleNamespace(
        runtime_manager=runtime,
        profiles=MagicMock(),
        setup=MagicMock(),
        local_proxy=MagicMock(),
        doctor=MagicMock(),
    )
    return selected


def test_profile_list_calls_service_once():
    services = api_services()
    services.profiles.list.return_value = [legacy_profile(), managed_profile()]

    with TestClient(create_app(services=services)) as client:
        response = client.get("/profiles")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["server-a", "managed-a"]
    assert response.json()[1]["key_id"] == "a" * 32
    services.profiles.list.assert_called_once_with()


def test_profile_get_calls_service():
    services = api_services()
    services.profiles.get.return_value = legacy_profile()

    with TestClient(create_app(services=services)) as client:
        response = client.get("/profiles/server-a")

    assert response.status_code == 200
    assert response.json()["name"] == "server-a"
    services.profiles.get.assert_called_once_with("server-a")


def test_profile_get_invalid_name_stops_before_service():
    services = api_services()

    with TestClient(create_app(services=services)) as client:
        response = client.get("/profiles/bad:name")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROFILE_INVALID"
    services.profiles.get.assert_not_called()


def test_profile_patch_passes_only_exact_provided_allowed_changes():
    services = api_services()
    services.profiles.update.return_value = legacy_profile()

    with TestClient(create_app(services=services)) as client:
        response = client.patch(
            "/profiles/server-a",
            json={"remote_port": 17891, "auto_reconnect": False},
        )

    assert response.status_code == 200
    services.profiles.update.assert_called_once_with(
        "server-a", {"remote_port": 17891, "auto_reconnect": False}
    )


def test_profile_patch_empty_preserves_service_error():
    services = api_services()
    services.profiles.update.side_effect = RABError("PROFILE_UPDATE_EMPTY", "no fields")

    with TestClient(create_app(services=services)) as client:
        response = client.patch("/profiles/server-a", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PROFILE_UPDATE_EMPTY"
    services.profiles.update.assert_called_once_with("server-a", {})


def test_profile_patch_forbidden_field_is_rejected_before_service():
    services = api_services()

    with TestClient(create_app(services=services)) as client:
        response = client.patch("/profiles/server-a", json={"key_id": "b" * 32})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_INVALID"
    services.profiles.update.assert_not_called()


@pytest.mark.parametrize("code", ["PROFILE_BUSY", "PROFILE_RUNTIME_ACTIVE"])
def test_profile_patch_active_profile_returns_409(code):
    services = api_services()
    services.profiles.update.side_effect = RABError(code, "profile is active", retryable=True)

    with TestClient(create_app(services=services)) as client:
        response = client.patch("/profiles/server-a", json={"remote_port": 17891})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == code


def test_delete_legacy_profile_returns_explicit_result():
    services = api_services()
    services.profiles.delete.return_value = ProfileDeleteResult(
        "server-a", stopped_owned_process=True, removed_stale_runtime=False
    )

    with TestClient(create_app(services=services)) as client:
        response = client.delete("/profiles/server-a")

    assert response.status_code == 200
    assert response.json() == {
        "name": "server-a",
        "stopped_owned_process": True,
        "removed_stale_runtime": False,
    }


def test_delete_managed_profile_preserves_credential_cleanup_refusal():
    services = api_services()
    services.profiles.delete.side_effect = RABError(
        "MANAGED_PROFILE_CREDENTIAL_CLEANUP_REQUIRED",
        "credential cleanup is required",
    )

    with TestClient(create_app(services=services)) as client:
        response = client.delete("/profiles/managed-a")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MANAGED_PROFILE_CREDENTIAL_CLEANUP_REQUIRED"
    services.profiles.delete.assert_called_once_with("managed-a")
    services.setup.assert_not_called()


def test_profile_not_found_remains_404():
    services = api_services()
    services.profiles.get.side_effect = RABError("PROFILE_NOT_FOUND", "not found")

    with TestClient(create_app(services=services)) as client:
        response = client.get("/profiles/missing")

    assert response.status_code == 404


def test_prepare_known_host_returns_host_preparation():
    services = api_services()
    services.setup.prepare.return_value = HostPreparation(
        "example.test", 22, "ssh-ed25519", "SHA256:" + "A" * 43, "READY_TO_AUTH"
    )

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/setup/host/prepare",
            json={"host": "example.test", "username": "alice"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "READY_TO_AUTH"
    services.setup.prepare.assert_called_once_with("example.test", "alice", 22)


def test_prepare_unknown_host_preserves_confirmation_details():
    services = api_services()
    fingerprint = "SHA256:" + "B" * 43
    services.setup.prepare.side_effect = RABError(
        "HOST_KEY_CONFIRMATION_REQUIRED",
        "confirmation required",
        details={
            "host": "example.test",
            "port": 22,
            "key_type": "ssh-ed25519",
            "fingerprint": fingerprint,
        },
    )

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/setup/host/prepare",
            json={"host": "example.test", "username": "alice"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["fingerprint"] == fingerprint


def test_confirm_host_key_calls_setup_service_once():
    services = api_services()
    fingerprint = "SHA256:" + "C" * 43
    services.setup.confirm_host_key.return_value = HostPreparation(
        "example.test", 22, "ssh-ed25519", fingerprint, "READY_TO_AUTH"
    )

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/setup/host/confirm",
            json={
                "host": "example.test",
                "port": 22,
                "expected_fingerprint": fingerprint,
                "accepted": True,
            },
        )

    assert response.status_code == 200
    services.setup.confirm_host_key.assert_called_once_with(
        "example.test", 22, fingerprint, accepted=True
    )


def test_host_key_changed_returns_409():
    services = api_services()
    services.setup.confirm_host_key.side_effect = RABError("HOST_KEY_CHANGED", "changed")

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/setup/host/confirm",
            json={
                "host": "example.test",
                "expected_fingerprint": "SHA256:" + "D" * 43,
                "accepted": True,
            },
        )

    assert response.status_code == 409


def test_local_proxy_discover_returns_selected_and_candidates():
    services = api_services()
    candidate = LocalProxyCandidate("127.0.0.1", 7897, True, True, True)
    services.local_proxy.discover.return_value = LocalProxyDiscovery(candidate, (candidate,))

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/setup/local-proxy/discover",
            json={"candidate_ports": [7897], "selected_local_proxy_port": 7897},
        )

    assert response.status_code == 200
    assert response.json()["selected"]["port"] == 7897
    assert response.json()["candidates"][0]["endpoint_reachable"] is True
    services.local_proxy.discover.assert_called_once_with(
        "https://api.openai.com/v1/models",
        candidate_ports=(7897,),
        selected_port=7897,
    )


def test_local_proxy_selection_required_preserves_candidates():
    services = api_services()
    candidates = [{"host": "127.0.0.1", "port": 7890}, {"host": "127.0.0.1", "port": 7897}]
    services.local_proxy.discover.side_effect = RABError(
        "LOCAL_PROXY_SELECTION_REQUIRED",
        "selection required",
        retryable=True,
        details={"candidates": candidates},
    )

    with TestClient(create_app(services=services)) as client:
        response = client.post("/setup/local-proxy/discover", json={})

    assert response.status_code == 409
    assert response.json()["error"]["details"]["candidates"] == candidates


def test_local_proxy_explicit_empty_candidates_are_not_replaced_by_defaults():
    services = api_services()
    services.local_proxy.discover.side_effect = RABError(
        "LOCAL_PROXY_DISCOVERY_INVALID", "candidate ports are required"
    )

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/setup/local-proxy/discover",
            json={"candidate_ports": []},
        )

    assert response.status_code == 400
    services.local_proxy.discover.assert_called_once_with(
        "https://api.openai.com/v1/models",
        candidate_ports=(),
        selected_port=None,
    )


def managed_setup_body(**changes):
    value = {
        "name": "managed-a",
        "host": "example.test",
        "username": "alice",
        "password": SECRET,
        "confirmed_fingerprint": "SHA256:" + "E" * 43,
        "selected_local_proxy_port": 7897,
        "candidate_proxy_ports": [7897],
    }
    value.update(changes)
    return value


def test_managed_setup_calls_service_once_and_never_returns_password():
    services = api_services()
    services.setup.setup_managed_profile.return_value = managed_profile()

    with TestClient(create_app(services=services)) as client:
        response = client.post("/setup/managed", json=managed_setup_body())

    assert response.status_code == 200
    assert response.json()["profile_type"] == "managed"
    assert SECRET not in response.text
    args, kwargs = services.setup.setup_managed_profile.call_args
    assert args == ("managed-a", "example.test", "alice", SECRET)
    assert kwargs["candidate_proxy_ports"] == (7897,)


def test_managed_setup_explicit_empty_candidates_reach_service_validation():
    services = api_services()
    services.setup.setup_managed_profile.side_effect = RABError(
        "LOCAL_PROXY_DISCOVERY_INVALID", "candidate ports are required"
    )

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/setup/managed",
            json=managed_setup_body(candidate_proxy_ports=[]),
        )

    assert response.status_code == 400
    _, kwargs = services.setup.setup_managed_profile.call_args
    assert kwargs["candidate_proxy_ports"] == ()


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("PROFILE_EXISTS", 409),
        ("SSH_BOOTSTRAP_FAILED", 503),
        ("SETUP_ROLLBACK_FAILED", 500),
    ],
)
def test_managed_setup_errors_are_mapped_without_password(code, status):
    services = api_services()
    services.setup.setup_managed_profile.side_effect = RABError(code, "setup failed")

    with TestClient(create_app(services=services)) as client:
        response = client.post("/setup/managed", json=managed_setup_body())

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert SECRET not in response.text


def test_request_validation_error_never_echoes_password_or_raw_input():
    services = api_services()
    body = managed_setup_body(unexpected=SECRET)

    with TestClient(create_app(services=services)) as client:
        response = client.post("/setup/managed", json=body)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_INVALID"
    assert SECRET not in response.text
    assert all(
        set(item) == {"loc", "msg", "type"}
        for item in response.json()["error"]["details"]["errors"]
    )
    services.setup.setup_managed_profile.assert_not_called()


def test_unexpected_setup_exception_never_leaks_password_to_response_or_log(caplog):
    services = api_services()
    services.setup.setup_managed_profile.side_effect = RuntimeError(SECRET)

    with TestClient(create_app(services=services), raise_server_exceptions=False) as client:
        response = client.post("/setup/managed", json=managed_setup_body())

    assert response.status_code == 500
    assert SECRET not in response.text
    assert SECRET not in caplog.text


def test_unexpected_setup_exception_is_consumed_before_asgi_server_logging(caplog):
    services = api_services()
    services.setup.setup_managed_profile.side_effect = RuntimeError(SECRET)

    with TestClient(create_app(services=services), raise_server_exceptions=True) as client:
        response = client.post("/setup/managed", json=managed_setup_body())

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_SERVER_ERROR",
            "message": "internal server error",
            "retryable": False,
            "details": {},
        }
    }
    assert SECRET not in response.text
    assert SECRET not in caplog.text


def doctor_report(secret_detail: str = "ok") -> DoctorReport:
    passed = CheckResult("pass", CheckStatus.PASS, secret_detail, http_status=401)
    failed = CheckResult("fail", CheckStatus.FAIL, "failed", "CHECK_FAILED")
    skipped = CheckResult("skip", CheckStatus.SKIP, "skipped")
    return DoctorReport(
        local=LocalProxyReport(passed, failed, skipped),
        ssh=passed,
        tunnel=failed,
        remote_listener=skipped,
        remote_endpoint=passed,
    )


def test_doctor_calls_profile_service_then_doctor_and_serializes_statuses():
    services = api_services()
    profile = legacy_profile()
    services.profiles.get.return_value = profile
    services.doctor.run.return_value = doctor_report()

    with TestClient(create_app(services=services)) as client:
        response = client.post("/doctor/server-a")

    assert response.status_code == 200
    services.profiles.get.assert_called_once_with("server-a")
    services.doctor.run.assert_called_once_with(profile)
    assert response.json()["local"]["tcp"]["status"] == "PASS"
    assert response.json()["local"]["handshake"]["status"] == "FAIL"
    assert response.json()["local"]["endpoint"]["status"] == "SKIP"
    assert response.json()["ssh"]["http_status"] == 401


def test_doctor_profile_not_found_returns_404_without_probe():
    services = api_services()
    services.profiles.get.side_effect = RABError("PROFILE_NOT_FOUND", "missing")

    with TestClient(create_app(services=services)) as client:
        response = client.post("/doctor/missing")

    assert response.status_code == 404
    services.doctor.run.assert_not_called()


def test_doctor_invalid_name_stops_before_services():
    services = api_services()

    with TestClient(create_app(services=services)) as client:
        response = client.post("/doctor/bad:name")

    assert response.status_code == 422
    services.profiles.get.assert_not_called()
    services.doctor.run.assert_not_called()


def test_doctor_redacts_diagnostic_detail():
    services = api_services()
    services.profiles.get.return_value = legacy_profile()
    services.doctor.run.return_value = doctor_report("password=supersecret")

    with TestClient(create_app(services=services)) as client:
        response = client.post("/doctor/server-a")

    assert response.status_code == 200
    assert "supersecret" not in response.text
    assert response.json()["ssh"]["detail"] == "password=[REDACTED]"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("PROFILE_UPDATE_EMPTY", 422),
        ("PROFILE_FIELD_NOT_UPDATABLE", 422),
        ("PROFILE_RUNTIME_ACTIVE", 409),
        ("PROFILE_DELETE_REMOTE_STATE_UNRESOLVED", 409),
        ("PROFILE_DELETE_RUNTIME_REMAINS", 409),
        ("PROFILE_DELETE_TUNNEL_STOP_FAILED", 503),
        ("HOST_KEY_CONFIRMATION_REQUIRED", 409),
        ("HOST_KEY_CHANGED", 409),
        ("LOCAL_PROXY_SELECTION_REQUIRED", 409),
        ("MANAGED_SETUP_UNAVAILABLE", 503),
        ("SETUP_ROLLBACK_FAILED", 500),
        ("SSH_BOOTSTRAP_FAILED", 503),
    ],
)
def test_phase252_error_mapping(code, status):
    services = api_services()
    services.profiles.list.side_effect = RABError(code, "failed")

    with TestClient(create_app(services=services)) as client:
        response = client.get("/profiles")

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
