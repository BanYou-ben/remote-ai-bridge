from __future__ import annotations

import json

import pytest

from app.domain.application_target import (
    ApplicationTarget,
    ApplicationTargetValidationError,
)


def _target(**changes) -> ApplicationTarget:
    values = {
        "schema_version": 1,
        "name": "rab-api",
        "profile_name": "server",
        "service_unit": "rab-api.service",
        "health_url": None,
    }
    values.update(changes)
    return ApplicationTarget(**values)


def test_valid_service_only_target() -> None:
    _target().validate()


def test_valid_health_only_target() -> None:
    _target(service_unit=None, health_url="http://127.0.0.1:8000/health").validate()


def test_valid_target_with_service_and_health() -> None:
    _target(health_url="https://127.0.0.1:8443/actuator/health").validate()


def test_target_without_diagnostic_capability_is_rejected() -> None:
    with pytest.raises(ApplicationTargetValidationError, match="requires"):
        _target(service_unit=None, health_url=None).validate()


@pytest.mark.parametrize(
    "name",
    ["", "bad name", "../bad", "bad/name", r"bad\name", "a..b", "bad;name", "-bad"],
)
def test_invalid_target_names_are_rejected(name: str) -> None:
    with pytest.raises(ApplicationTargetValidationError, match="target name"):
        _target(name=name).validate()


@pytest.mark.parametrize("profile_name", ["", "bad/name", "bad name", "../server"])
def test_invalid_profile_names_use_profile_validation(profile_name: str) -> None:
    with pytest.raises(ApplicationTargetValidationError, match="profile name"):
        _target(profile_name=profile_name).validate()


@pytest.mark.parametrize("service_unit", ["nginx.service", "rab-api.service", "foo@1.service"])
def test_valid_systemd_service_units_are_accepted(service_unit: str) -> None:
    _target(service_unit=service_unit).validate()


@pytest.mark.parametrize(
    "service_unit",
    [
        "../../etc.service",
        "a;rm.service",
        '"a.service"',
        "a service.service",
        "a/service.service",
        "a|b.service",
        "a$b.service",
        "a\n.service",
    ],
)
def test_unsafe_service_units_are_rejected(service_unit: str) -> None:
    with pytest.raises(ApplicationTargetValidationError, match="service unit"):
        _target(service_unit=service_unit).validate()


@pytest.mark.parametrize(
    "health_url",
    [
        "http://127.0.0.1:8000/health",
        "https://127.0.0.1:8443/actuator/health",
    ],
)
def test_loopback_health_urls_with_explicit_ports_are_accepted(health_url: str) -> None:
    _target(service_unit=None, health_url=health_url).validate()


@pytest.mark.parametrize(
    "health_url",
    [
        "http://127.0.0.1/health",
        "http://127.0.0.1:0/health",
        "http://127.0.0.1:65536/health",
        "http://127.0.0.1:bad/health",
        "http://10.0.0.1:8000/health",
        "http://169.254.169.254:80/latest/meta-data",
        "https://example.com:443/health",
        "http://localhost:8000/health",
        "http://user:pass@127.0.0.1:8000/",
        "http://127.0.0.1:8000/x?a=1",
        "http://127.0.0.1:8000/#x",
        "ftp://127.0.0.1:21/health",
        "http://127.0.0.1:8000/bad\npath",
        "http://127.0.0.1:8000/bad\tpath",
        r"http://127.0.0.1:8000/bad\path",
    ],
)
def test_unsafe_or_non_loopback_health_urls_are_rejected(health_url: str) -> None:
    with pytest.raises(ApplicationTargetValidationError, match="health URL"):
        _target(service_unit=None, health_url=health_url).validate()


def test_unknown_fields_are_rejected() -> None:
    payload = _target().to_dict()
    payload["token"] = "secret"
    with pytest.raises(ApplicationTargetValidationError, match="fields"):
        ApplicationTarget.from_dict(payload)


def test_missing_fields_are_rejected() -> None:
    payload = _target().to_dict()
    payload.pop("health_url")
    with pytest.raises(ApplicationTargetValidationError, match="fields"):
        ApplicationTarget.from_dict(payload)


@pytest.mark.parametrize("schema_version", [1.0, True, "1", None, 0, 2])
def test_unsupported_schema_versions_are_rejected(schema_version: object) -> None:
    with pytest.raises(ApplicationTargetValidationError, match="schema_version"):
        _target(schema_version=schema_version).validate()


def test_round_trip_and_serialization_are_deterministic() -> None:
    target = _target(health_url="http://127.0.0.1:8000/health")
    payload = target.to_dict()
    restored = ApplicationTarget.from_dict(payload)
    assert restored == target
    assert list(payload) == [
        "schema_version",
        "name",
        "profile_name",
        "service_unit",
        "health_url",
    ]
    assert json.dumps(payload, sort_keys=True) == json.dumps(
        restored.to_dict(),
        sort_keys=True,
    )


def test_target_contract_has_no_secret_or_remote_connection_fields() -> None:
    fields = set(_target().to_dict())
    assert fields == {
        "schema_version",
        "name",
        "profile_name",
        "service_unit",
        "health_url",
    }
    assert fields.isdisjoint(
        {"password", "token", "api_key", "authorization", "cookie", "private_key", "host", "port"}
    )
