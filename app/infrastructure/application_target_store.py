from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from app.domain.application_target import (
    ApplicationTarget,
    validate_application_target_name,
)
from app.infrastructure.profile_store import default_state_root


class ApplicationTargetNotFoundError(FileNotFoundError):
    pass


class ApplicationTargetStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_state_root()
        self.targets_dir = self.root / "application_targets"

    def save_target(self, target: ApplicationTarget, overwrite: bool = False) -> None:
        target.validate()
        path = self._target_path(target.name)
        if path.exists() and not overwrite:
            raise FileExistsError(f"application target already exists: {target.name}")
        self._write_json(path, target.to_dict())

    def load_target(self, name: str) -> ApplicationTarget:
        path = self._target_path(name)
        if not path.exists():
            raise ApplicationTargetNotFoundError(f"application target not found: {name}")
        return ApplicationTarget.from_dict(self._read_json(path))

    def target_exists(self, name: str) -> bool:
        return self._target_path(name).exists()

    def update_target(self, target: ApplicationTarget) -> None:
        target.validate()
        path = self._target_path(target.name)
        if not path.exists():
            raise ApplicationTargetNotFoundError(
                f"application target not found: {target.name}"
            )
        self._write_json(path, target.to_dict())

    def delete_target(self, name: str) -> None:
        path = self._target_path(name)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ApplicationTargetNotFoundError(
                f"application target not found: {name}"
            ) from exc

    def list_targets(self) -> list[ApplicationTarget]:
        self._ensure_safe_directory()
        if not self.targets_dir.exists():
            return []
        return [
            ApplicationTarget.from_dict(self._read_json(path))
            for path in sorted(self.targets_dir.glob("*.json"))
        ]

    def _target_path(self, name: str) -> Path:
        validate_application_target_name(name)
        self._ensure_safe_directory()
        return self.targets_dir / f"{name}.json"

    def _ensure_safe_directory(self) -> None:
        if self.targets_dir.is_symlink():
            raise ValueError("application target directory must not be a symbolic link")
        try:
            attributes = getattr(self.targets_dir.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            return
        reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if reparse_point and attributes & reparse_point:
            raise ValueError("application target directory must not be a symbolic link")

    def _read_json(self, path: Path) -> dict[str, object]:
        self._ensure_safe_directory()
        if path.is_symlink():
            raise ValueError("application target file must not be a symbolic link")
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"expected JSON object in {path}")
        return data

    def _write_json(self, path: Path, data: dict[str, object]) -> None:
        self._ensure_safe_directory()
        self.targets_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_safe_directory()
        if path.exists() and path.is_symlink():
            raise ValueError("application target file must not be a symbolic link")
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=self.targets_dir,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary_name, path)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
