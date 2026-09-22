from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager
from typing import Iterator

from app.domain.profile import Profile, RuntimeState


class ProfileNotFoundError(FileNotFoundError):
    pass


class ProfileLockError(RuntimeError):
    pass


def default_state_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "RemoteAIBridge"
    return Path.home() / ".remote-ai-bridge-windows"


class ProfileStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_state_root()
        self.profiles_dir = self.root / "profiles"
        self.runtime_dir = self.root / "runtime"

    def save_profile(self, profile: Profile, overwrite: bool = False) -> None:
        profile.validate()
        path = self._profile_path(profile.name)
        if path.exists() and not overwrite:
            raise FileExistsError(f"profile already exists: {profile.name}")
        self._write_json(path, profile.to_dict())

    def load_profile(self, name: str) -> Profile:
        path = self._profile_path(name)
        if not path.exists():
            raise ProfileNotFoundError(f"profile not found: {name}")
        return Profile.from_dict(self._read_json(path))

    def profile_exists(self, name: str) -> bool:
        return self._profile_path(name).exists()

    def update_profile(self, profile: Profile) -> None:
        profile.validate()
        path = self._profile_path(profile.name)
        if not path.exists():
            raise ProfileNotFoundError(f"profile not found: {profile.name}")
        self._write_json(path, profile.to_dict())

    def delete_profile(self, name: str) -> None:
        path = self._profile_path(name)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ProfileNotFoundError(f"profile not found: {name}") from exc

    def list_profiles(self) -> list[Profile]:
        if not self.profiles_dir.exists():
            return []
        return [Profile.from_dict(self._read_json(path)) for path in sorted(self.profiles_dir.glob("*.json"))]

    def save_runtime(self, state: RuntimeState) -> None:
        state.validate()
        self._write_json(self._runtime_path(state.profile_name), state.to_dict())

    def load_runtime(self, name: str) -> RuntimeState | None:
        path = self._runtime_path(name)
        if not path.exists():
            return None
        return RuntimeState.from_dict(self._read_json(path))

    def clear_runtime(self, name: str) -> None:
        path = self._runtime_path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @contextmanager
    def supervisor_lock(self, name: str) -> Iterator[None]:
        path = self._lock_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                _lock_file(handle)
            except OSError as exc:
                raise ProfileLockError(f"profile '{name}' already has an active foreground supervisor") from exc
            try:
                yield
            finally:
                handle.seek(0)
                _unlock_file(handle)
        finally:
            handle.close()

    def _profile_path(self, name: str) -> Path:
        if not name or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in name):
            raise ValueError("unsafe profile name")
        return self.profiles_dir / f"{name}.json"

    def _runtime_path(self, name: str) -> Path:
        if not name or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in name):
            raise ValueError("unsafe profile name")
        return self.runtime_dir / f"{name}.json"

    def _lock_path(self, name: str) -> Path:
        self._runtime_path(name)
        return self.runtime_dir / f"{name}.lock"

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"expected JSON object in {path}")
        return data

    @staticmethod
    def _write_json(path: Path, data: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
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


def _lock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
