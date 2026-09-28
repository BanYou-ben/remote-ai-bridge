from __future__ import annotations

import pytest

from app.domain.application_target import ApplicationTarget
from app.domain.errors import RABError
from app.infrastructure.application_target_store import ApplicationTargetStore
from app.services.application_target_service import ApplicationTargetService


def _target(name: str = "rab-api", **changes) -> ApplicationTarget:
    values = {
        "schema_version": 1,
        "name": name,
        "profile_name": "server",
        "service_unit": "rab-api.service",
        "health_url": None,
    }
    values.update(changes)
    return ApplicationTarget(**values)


class FakeProfiles:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = existing or {"server"}
        self.calls: list[str] = []

    def get(self, name: str):
        self.calls.append(name)
        if name not in self.existing:
            raise RABError("PROFILE_NOT_FOUND", "profile not found", details={"name": name})
        return object()


def _service(tmp_path, existing: set[str] | None = None):
    profiles = FakeProfiles(existing)
    store = ApplicationTargetStore(tmp_path)
    return ApplicationTargetService(store, profiles), store, profiles


def test_service_create_verifies_profile_before_persisting(tmp_path) -> None:
    service, store, profiles = _service(tmp_path)
    created = service.create(_target())
    assert created == _target()
    assert profiles.calls == ["server"]
    assert store.load_target("rab-api") == created


def test_service_rejects_orphan_target_without_persisting(tmp_path) -> None:
    service, store, profiles = _service(tmp_path, {"other"})
    with pytest.raises(RABError) as captured:
        service.create(_target())
    assert captured.value.code == "APPLICATION_TARGET_PROFILE_NOT_FOUND"
    assert profiles.calls == ["server"]
    assert store.target_exists("rab-api") is False


def test_service_rejects_duplicate_target(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    service.create(_target())
    with pytest.raises(RABError) as captured:
        service.create(_target())
    assert captured.value.code == "APPLICATION_TARGET_EXISTS"


def test_service_get_and_list(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    service.create(_target("zeta"))
    service.create(_target("alpha"))
    assert service.get("alpha").name == "alpha"
    assert [item.name for item in service.list()] == ["alpha", "zeta"]


def test_service_get_missing_target_has_stable_error(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    with pytest.raises(RABError) as captured:
        service.get("missing")
    assert captured.value.code == "APPLICATION_TARGET_NOT_FOUND"


def test_service_update_verifies_new_profile_before_persisting(tmp_path) -> None:
    service, store, profiles = _service(tmp_path, {"server", "server-b"})
    service.create(_target())
    updated = service.update(
        "rab-api",
        {
            "profile_name": "server-b",
            "service_unit": None,
            "health_url": "http://127.0.0.1:9000/health",
        },
    )
    assert updated.profile_name == "server-b"
    assert updated.service_unit is None
    assert store.load_target("rab-api") == updated
    assert profiles.calls == ["server", "server-b"]


def test_service_update_to_missing_profile_preserves_target(tmp_path) -> None:
    service, store, _ = _service(tmp_path)
    original = service.create(_target())
    with pytest.raises(RABError) as captured:
        service.update("rab-api", {"profile_name": "missing"})
    assert captured.value.code == "APPLICATION_TARGET_PROFILE_NOT_FOUND"
    assert store.load_target("rab-api") == original


def test_service_update_rejects_empty_and_non_updatable_fields(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    service.create(_target())
    with pytest.raises(RABError) as empty:
        service.update("rab-api", {})
    assert empty.value.code == "APPLICATION_TARGET_UPDATE_EMPTY"
    for changes in ({"name": "renamed"}, {"schema_version": 2}, {"token": "secret"}):
        with pytest.raises(RABError) as invalid:
            service.update("rab-api", changes)
        assert invalid.value.code == "APPLICATION_TARGET_FIELD_NOT_UPDATABLE"


def test_service_update_rejects_target_without_capability(tmp_path) -> None:
    service, store, _ = _service(tmp_path)
    original = service.create(_target())
    with pytest.raises(RABError) as captured:
        service.update("rab-api", {"service_unit": None, "health_url": None})
    assert captured.value.code == "APPLICATION_TARGET_INVALID"
    assert store.load_target("rab-api") == original


def test_service_update_missing_target_has_stable_error(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    with pytest.raises(RABError) as captured:
        service.update("missing", {"service_unit": "nginx.service"})
    assert captured.value.code == "APPLICATION_TARGET_NOT_FOUND"


def test_service_delete_removes_target_only(tmp_path) -> None:
    service, store, profiles = _service(tmp_path)
    service.create(_target())
    service.delete("rab-api")
    assert store.target_exists("rab-api") is False
    assert profiles.calls == ["server"]


def test_service_delete_missing_target_has_stable_error(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    with pytest.raises(RABError) as captured:
        service.delete("missing")
    assert captured.value.code == "APPLICATION_TARGET_NOT_FOUND"


def test_service_maps_invalid_target_to_structured_error(tmp_path) -> None:
    service, store, profiles = _service(tmp_path)
    with pytest.raises(RABError) as captured:
        service.create(_target(service_unit="bad;unit.service"))
    assert captured.value.code == "APPLICATION_TARGET_INVALID"
    assert profiles.calls == []
    assert store.target_exists("rab-api") is False


def test_service_propagates_non_not_found_profile_errors(tmp_path) -> None:
    class BrokenProfiles(FakeProfiles):
        def get(self, name: str):
            raise RABError("PROFILE_STORE_FAILED", "profile store unavailable", retryable=True)

    store = ApplicationTargetStore(tmp_path)
    service = ApplicationTargetService(store, BrokenProfiles())
    with pytest.raises(RABError) as captured:
        service.create(_target())
    assert captured.value.code == "PROFILE_STORE_FAILED"
    assert store.target_exists("rab-api") is False
