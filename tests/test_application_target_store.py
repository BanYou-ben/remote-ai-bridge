from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from app.domain.application_target import (
    ApplicationTarget,
    ApplicationTargetValidationError,
)
from app.infrastructure.application_target_store import (
    ApplicationTargetNotFoundError,
    ApplicationTargetStore,
)


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


def _create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 1314:
            raise
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
        )
        if completed.returncode != 0:
            raise OSError("unable to create test directory link") from exc


def test_store_saves_and_loads_target(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    target = _target()
    store.save_target(target)
    assert store.load_target(target.name) == target
    assert store.target_exists(target.name) is True
    assert (tmp_path / "application_targets" / "rab-api.json").is_file()


def test_store_rejects_duplicate_create_without_overwrite(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    store.save_target(_target())
    with pytest.raises(FileExistsError):
        store.save_target(_target(health_url="http://127.0.0.1:8000/health"))
    assert store.load_target("rab-api").health_url is None


def test_store_updates_existing_target_atomically(tmp_path, monkeypatch) -> None:
    store = ApplicationTargetStore(tmp_path)
    store.save_target(_target())
    original_replace = os.replace
    replacements: list[tuple[str, str]] = []

    def observe(source, destination):
        replacements.append((str(source), str(destination)))
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", observe)
    updated = _target(health_url="http://127.0.0.1:8000/health")
    store.update_target(updated)
    assert store.load_target("rab-api") == updated
    assert len(replacements) == 1
    assert replacements[0][1].endswith("rab-api.json")


def test_store_update_requires_existing_target(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    with pytest.raises(ApplicationTargetNotFoundError):
        store.update_target(_target())


def test_store_deletes_only_selected_target(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    store.save_target(_target("alpha"))
    store.save_target(_target("beta"))
    store.delete_target("alpha")
    assert store.target_exists("alpha") is False
    assert store.load_target("beta").name == "beta"


def test_store_delete_and_load_report_missing_target(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    with pytest.raises(ApplicationTargetNotFoundError):
        store.load_target("missing")
    with pytest.raises(ApplicationTargetNotFoundError):
        store.delete_target("missing")


def test_store_lists_targets_deterministically(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    for name in ("zeta", "alpha", "middle"):
        store.save_target(_target(name))
    assert [item.name for item in store.list_targets()] == ["alpha", "middle", "zeta"]
    assert [item.name for item in store.list_targets()] == ["alpha", "middle", "zeta"]


@pytest.mark.parametrize(
    "name",
    ["../profiles/server", "..", "a..b", "bad/name", r"bad\name", "bad name"],
)
def test_store_rejects_traversal_and_unsafe_names_before_path_use(tmp_path, name: str) -> None:
    store = ApplicationTargetStore(tmp_path)
    with pytest.raises(ApplicationTargetValidationError):
        store.load_target(name)
    assert not (tmp_path / "profiles").exists()


def test_store_rejects_malformed_json(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    store.targets_dir.mkdir(parents=True)
    (store.targets_dir / "broken.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        store.load_target("broken")


def test_store_rejects_invalid_persisted_target(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    store.targets_dir.mkdir(parents=True)
    payload = _target().to_dict()
    payload["health_url"] = "http://169.254.169.254:80/latest/meta-data"
    (store.targets_dir / "rab-api.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    with pytest.raises(ApplicationTargetValidationError):
        store.load_target("rab-api")


def test_store_uses_utf8_json_for_safe_unicode_url_path(tmp_path) -> None:
    store = ApplicationTargetStore(tmp_path)
    target = _target(
        service_unit=None,
        health_url="http://127.0.0.1:8000/健康",
    )
    store.save_target(target)
    raw = (store.targets_dir / "rab-api.json").read_text(encoding="utf-8")
    assert "健康" in raw
    assert store.load_target("rab-api") == target


def test_store_rejects_symlink_target_file(tmp_path, monkeypatch) -> None:
    store = ApplicationTargetStore(tmp_path)
    store.targets_dir.mkdir(parents=True)
    path = store.targets_dir / "rab-api.json"
    path.write_text("{}", encoding="utf-8")
    original = type(path).is_symlink

    def fake_is_symlink(candidate):
        if candidate == path:
            return True
        return original(candidate)

    monkeypatch.setattr(type(path), "is_symlink", fake_is_symlink)
    with pytest.raises(ValueError, match="symbolic link"):
        store.load_target("rab-api")


def test_store_delete_rejects_symlink_directory_without_deleting_external_file(
    tmp_path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    external_target = outside / "rab-api.json"
    external_target.write_text(json.dumps(_target().to_dict()), encoding="utf-8")

    store = ApplicationTargetStore(tmp_path / "state")
    store.root.mkdir()
    _create_directory_link(store.targets_dir, outside)

    with pytest.raises(ValueError, match="symbolic link"):
        store.delete_target("rab-api")

    assert external_target.is_file()


@pytest.mark.parametrize(
    "operation",
    ["load", "exists", "update", "save", "list"],
)
def test_store_public_operations_reject_symlink_directory(tmp_path, operation: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "rab-api.json").write_text(
        json.dumps(_target().to_dict()),
        encoding="utf-8",
    )

    store = ApplicationTargetStore(tmp_path / "state")
    store.root.mkdir()
    _create_directory_link(store.targets_dir, outside)
    operations = {
        "load": lambda: store.load_target("rab-api"),
        "exists": lambda: store.target_exists("rab-api"),
        "update": lambda: store.update_target(_target()),
        "save": lambda: store.save_target(_target(), overwrite=True),
        "list": store.list_targets,
    }

    with pytest.raises(ValueError, match="symbolic link"):
        operations[operation]()
