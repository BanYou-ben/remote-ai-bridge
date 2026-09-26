from __future__ import annotations

import json
from dataclasses import replace

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.domain.supervisor import SupervisorSnapshot, SupervisorState
from app.services.ai.context_builder import DiagnosticContextBuilder
from app.services.ai.schemas import DiagnosticContext
from app.services.doctor import DoctorReport
from app.services.local_proxy import LocalProxyReport


def managed_profile() -> Profile:
    return Profile(
        schema_version=2,
        name="server-a",
        ssh_target="alice@example.test",
        local_proxy_port=7897,
        remote_port=17890,
        auto_reconnect=True,
        profile_type="managed",
        host="example.test",
        username="alice",
        ssh_port=22,
        key_id="a" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:" + "A" * 43,
    )


def runtime_snapshot(*, message: str = "bridge is unhealthy") -> SupervisorSnapshot:
    return SupervisorSnapshot(
        profile_name="server-a",
        state=SupervisorState.FAILED,
        updated_at="2026-09-26T00:00:00+00:00",
        attempt=2,
        error_code="REMOTE_ENDPOINT_UNREACHABLE",
        message=message,
        retry_in_seconds=None,
        pid=4321,
        tunnel_id="private-tunnel-identity",
        remote_port=17890,
        last_successful_probe_at=None,
        suggested_port=17891,
        supervised=True,
        runtime_present=True,
        process_alive=False,
    )


def doctor_report(*, detail: str = "check detail") -> DoctorReport:
    return DoctorReport(
        local=LocalProxyReport(
            CheckResult("TCP reachable", CheckStatus.PASS, detail),
            CheckResult("HTTP proxy handshake", CheckStatus.FAIL, "handshake failed", "HANDSHAKE_FAILED"),
            CheckResult("AI endpoint probe", CheckStatus.SKIP, "handshake unavailable"),
        ),
        ssh=CheckResult("SSH configuration", CheckStatus.PASS, "target resolves"),
        tunnel=CheckResult("Tunnel process", CheckStatus.FAIL, "process exited", "PROCESS_EXITED"),
        remote_listener=CheckResult("Remote listener", CheckStatus.SKIP, "tunnel unavailable"),
        remote_endpoint=CheckResult(
            "Remote endpoint probe",
            CheckStatus.FAIL,
            "endpoint returned HTTP 503",
            "ENDPOINT_UNEXPECTED_STATUS",
            503,
        ),
    )


def build_context(
    *,
    message: str = "bridge is unhealthy",
    detail: str = "check detail",
) -> DiagnosticContext:
    return DiagnosticContextBuilder().build(
        managed_profile(),
        runtime_snapshot(message=message),
        doctor_report(detail=detail),
    )


def test_context_builder_converts_profile_runtime_and_doctor_report() -> None:
    payload = build_context().to_dict()

    assert payload["profile"] == {
        "name": "server-a",
        "profile_type": "managed",
        "local_proxy_host": "127.0.0.1",
        "local_proxy_port": 7897,
        "remote_bind_host": "127.0.0.1",
        "remote_port": 17890,
        "auto_reconnect": True,
        "endpoint_probe_url": "https://api.openai.com/v1/models",
    }
    assert payload["runtime"]["attempt"] == 2
    assert len(payload["evidence"]) == 7
    assert payload["evidence"][0]["category"] == "connection"
    json.dumps(payload)


def test_evidence_ids_are_fixed_and_ordered() -> None:
    evidence = build_context().to_dict()["evidence"]

    assert [item["id"] for item in evidence] == [
        "connection.proxy.tcp",
        "connection.proxy.handshake",
        "connection.proxy.endpoint",
        "connection.ssh.config",
        "connection.tunnel.process",
        "connection.remote.listener",
        "connection.remote.endpoint",
    ]


def test_all_doctor_evidence_uses_connection_category() -> None:
    evidence = build_context().to_dict()["evidence"]

    assert [item["category"] for item in evidence] == ["connection"] * 7


def test_check_statuses_remain_pass_fail_and_skip() -> None:
    evidence = build_context().to_dict()["evidence"]

    assert [item["status"] for item in evidence[:3]] == ["PASS", "FAIL", "SKIP"]


def test_evidence_preserves_error_code_and_http_status() -> None:
    remote_endpoint = build_context().to_dict()["evidence"][-1]

    assert remote_endpoint["error_code"] == "ENDPOINT_UNEXPECTED_STATUS"
    assert remote_endpoint["http_status"] == 503


def test_runtime_state_serializes_as_plain_string() -> None:
    runtime = build_context().to_dict()["runtime"]

    assert runtime["state"] == "FAILED"
    assert "SupervisorState" not in json.dumps(runtime)


def test_context_excludes_managed_credentials_and_process_identity() -> None:
    profile = replace(
        managed_profile(),
        endpoint_probe_url="https://api.openai.com/api_key=endpoint-secret",
    )
    context = DiagnosticContextBuilder().build(
        profile,
        runtime_snapshot(),
        doctor_report(),
    )
    serialized = json.dumps(context.to_dict())

    for forbidden in (
        "key_id",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "host_key_fingerprint",
        "host_key_type",
        "SHA256:",
        "ssh-ed25519",
        "password",
        "private_key",
        "SecretStr",
        "ssh_target",
        "example.test",
        "alice",
        "pid",
        "tunnel_id",
        "private-tunnel-identity",
        "endpoint-secret",
    ):
        assert forbidden not in serialized
    assert "[REDACTED]" in serialized


def test_runtime_message_and_doctor_detail_use_shared_redaction() -> None:
    private_material = "TOP-SECRET-PRIVATE-MATERIAL"
    context = build_context(
        message="password=hunter2 Authorization: Bearer runtime-token",
        detail=(
            "api_key=doctor-key\n"
            "-----BEGIN OPENSSH PRIVATE KEY-----\n"
            f"{private_material}\n"
            "-----END OPENSSH PRIVATE KEY-----"
        ),
    )
    serialized = json.dumps(context.to_dict())

    for secret in ("hunter2", "runtime-token", "doctor-key", private_material):
        assert secret not in serialized
    assert "[REDACTED]" in serialized


def test_context_serialization_is_deterministic() -> None:
    builder = DiagnosticContextBuilder()
    profile = managed_profile()
    runtime = runtime_snapshot()
    doctor = doctor_report()

    assert builder.build(profile, runtime, doctor).to_dict() == builder.build(
        profile,
        runtime,
        doctor,
    ).to_dict()
