from __future__ import annotations

import json
import socket

import pytest

from app.domain.health import CheckStatus
from app.domain.profile import Profile
from app.services.network_transport import NetworkTransportService


def _managed_profile() -> Profile:
    return Profile(
        schema_version=2,
        name="server",
        ssh_target="alice@ssh.example.test",
        local_proxy_port=7897,
        remote_port=17890,
        profile_type="managed",
        host="ssh.example.test",
        username="alice",
        ssh_port=2222,
        key_id="1" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:" + "A" * 43,
    )


def _legacy_profile() -> Profile:
    return Profile(schema_version=1, name="legacy", ssh_target="ssh-alias")


class FakeConnection:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_args):
        self.exited = True


def _resolved(monkeypatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(object(),)])


def test_ssh_transport_tcp_success(monkeypatch) -> None:
    calls: list[tuple[object, object]] = []
    connection = FakeConnection()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, **kwargs: calls.append((host, port)) or [(object(),)],
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda target, timeout: calls.append((target, timeout)) or connection,
    )
    report = NetworkTransportService(timeout=3).check_ssh_transport(_managed_profile())
    assert report.dns.status is CheckStatus.PASS
    assert report.tcp.status is CheckStatus.PASS
    assert calls == [
        ("ssh.example.test", 2222),
        (("ssh.example.test", 2222), 3.0),
    ]
    assert connection.entered and connection.exited


def test_dns_failure_skips_tcp_without_connecting(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(socket.gaierror("secret DNS detail")),
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    report = NetworkTransportService().check_ssh_transport(_managed_profile())
    assert (report.dns.status, report.dns.error_code) == (
        CheckStatus.FAIL,
        "SSH_DNS_RESOLUTION_FAILED",
    )
    assert report.tcp.status is CheckStatus.SKIP


@pytest.mark.parametrize(
    ("error", "code", "detail"),
    [
        (ConnectionRefusedError("secret refused"), "SSH_TCP_REFUSED", "SSH TCP connection was refused"),
        (TimeoutError("secret timeout"), "SSH_TCP_TIMEOUT", "SSH TCP connection timed out"),
        (OSError("secret system path"), "SSH_TCP_UNREACHABLE", "SSH TCP endpoint is unreachable"),
    ],
)
def test_tcp_errors_are_stably_classified(monkeypatch, error, code: str, detail: str) -> None:
    _resolved(monkeypatch)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    report = NetworkTransportService().check_ssh_transport(_managed_profile())
    assert report.dns.status is CheckStatus.PASS
    assert report.tcp.status is CheckStatus.FAIL
    assert report.tcp.error_code == code
    assert report.tcp.detail == detail
    assert "secret" not in report.tcp.detail


def test_legacy_profile_target_is_not_guessed(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not resolve")),
    )
    report = NetworkTransportService().check_ssh_transport(_legacy_profile())
    assert report.dns.status is CheckStatus.SKIP
    assert report.tcp.status is CheckStatus.SKIP
    assert report.dns.error_code == "NETWORK_TARGET_UNAVAILABLE"
    assert report.tcp.error_code == "NETWORK_TARGET_UNAVAILABLE"


@pytest.mark.parametrize("timeout", [0, -1, 60.1, True, "5", None])
def test_timeout_has_safe_bounds(timeout: object) -> None:
    with pytest.raises(ValueError, match="timeout"):
        NetworkTransportService(timeout=timeout)


@pytest.mark.parametrize("timeout", [0.1, 5, 60])
def test_supported_timeout_values_are_normalized(timeout: float) -> None:
    assert NetworkTransportService(timeout=timeout).timeout == float(timeout)


def test_report_serialization_is_deterministic_and_safe(monkeypatch) -> None:
    _resolved(monkeypatch)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("RAB-SECRET")),
    )
    report = NetworkTransportService().check_ssh_transport(_managed_profile())
    payload = {
        "dns": report.dns.__dict__,
        "tcp": report.tcp.__dict__,
    }
    serialized = json.dumps(payload, default=lambda value: value.value, sort_keys=True)
    assert serialized == json.dumps(payload, default=lambda value: value.value, sort_keys=True)
    assert "RAB-SECRET" not in serialized
    assert "ssh.example.test" not in serialized
