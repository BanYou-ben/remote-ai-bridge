from __future__ import annotations

from app.domain.errors import RABError
from app.domain.profile import Profile, RuntimeState


class FakeProfiles:
    def __init__(self, *names: str) -> None:
        self.items = {name: Profile(1, name, name) for name in names}

    def get(self, name: str) -> Profile:
        try:
            return self.items[name]
        except KeyError:
            raise RABError("PROFILE_NOT_FOUND", f"profile not found: {name}") from None

    def list(self) -> list[Profile]:
        return list(self.items.values())


def runtime_state(*, profile_name: str = "server") -> RuntimeState:
    return RuntimeState(
        schema_version=1,
        profile_name=profile_name,
        pid=123,
        process_creation_time=1.0,
        executable_path="C:/Windows/System32/OpenSSH/ssh.exe",
        tunnel_id="tunnel-1",
        supervisor_pid=456,
        remote_port=17890,
        started_at="2026-01-01T00:00:00+00:00",
    )
