from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.domain.profile import Profile, RuntimeState
from app.domain.supervisor import SupervisorSnapshot, SupervisorState, utc_now
from app.infrastructure.profile_store import ProfileStore
from app.services.profile_service import ProfileService
from app.services.runtime_manager import RuntimeManager


def legacy_profile(name: str = "shared-profile") -> Profile:
    return Profile(1, name, name, local_proxy_port=7897, remote_port=17890)


def managed_profile(name: str = "api-managed") -> Profile:
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


def runtime_state(name: str = "shared-profile") -> RuntimeState:
    return RuntimeState(
        schema_version=1,
        profile_name=name,
        pid=123,
        process_creation_time=1.0,
        executable_path="C:/Windows/System32/OpenSSH/ssh.exe",
        tunnel_id="tunnel-phase26",
        supervisor_pid=456,
        remote_port=17890,
        started_at="2026-09-24T00:00:00+00:00",
    )


def api_services(**values):
    runtime = values.pop("runtime_manager", None)
    if runtime is None:
        runtime = MagicMock()
        runtime.shutdown.return_value = SimpleNamespace(completed=True, results=())
    defaults = {
        "runtime_manager": runtime,
        "profiles": MagicMock(),
        "setup": MagicMock(),
        "local_proxy": MagicMock(),
        "doctor": MagicMock(),
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_service_created_profile_is_visible_through_api_on_shared_state_root(tmp_path):
    store = ProfileStore(tmp_path)
    profiles = ProfileService(store, MagicMock())
    profiles.create(legacy_profile())
    services = api_services(profiles=profiles)

    with TestClient(create_app(services=services)) as client:
        response = client.get("/profiles")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["shared-profile"]


def test_api_managed_setup_persistence_is_readable_by_fresh_profile_service(tmp_path):
    store = ProfileStore(tmp_path)
    tunnel = MagicMock()
    profiles = ProfileService(store, tunnel)
    selected = managed_profile()
    setup = MagicMock()
    setup.setup_managed_profile.side_effect = lambda *args, **kwargs: profiles.create(selected)
    services = api_services(profiles=profiles, setup=setup)

    with TestClient(create_app(services=services)) as client:
        response = client.post(
            "/setup/managed",
            json={
                "name": "api-managed",
                "host": "example.test",
                "username": "alice",
                "password": "temporary-test-password",
                "confirmed_fingerprint": "SHA256:" + "A" * 43,
                "selected_local_proxy_port": 7897,
                "candidate_proxy_ports": [7897],
            },
        )

    fresh_profiles = ProfileService(ProfileStore(tmp_path), tunnel)
    assert response.status_code == 200
    assert fresh_profiles.get("api-managed") == selected


class PersistingSupervisor:
    def __init__(self, store: ProfileStore, name: str, ready: threading.Event) -> None:
        self.store = store
        self.name = name
        self.ready = ready
        self._snapshot = SupervisorSnapshot(name, SupervisorState.STARTING, utc_now())
        self._lock = threading.Lock()

    def snapshot(self) -> SupervisorSnapshot:
        with self._lock:
            return self._snapshot

    def run(self, stop_event: threading.Event) -> SupervisorSnapshot:
        self.store.save_runtime(runtime_state(self.name))
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                state=SupervisorState.READY,
                updated_at=utc_now(),
                message="ready",
                runtime_present=True,
                process_alive=True,
            )
        self.ready.set()
        stop_event.wait(2.0)
        self.store.clear_runtime(self.name)
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                state=SupervisorState.STOPPED,
                updated_at=utc_now(),
                message="stopped",
                runtime_present=False,
                process_alive=False,
            )
        return self._snapshot

    def fail_unexpected(self, exc: Exception) -> None:
        raise AssertionError(f"unexpected fake supervisor failure: {exc}")


def test_api_runtime_is_visible_to_fresh_manager_and_disconnect_clears_evidence(tmp_path):
    store = ProfileStore(tmp_path)
    tunnel = MagicMock()
    tunnel.inspector.matches.return_value = True
    profiles = ProfileService(store, tunnel)
    profiles.create(legacy_profile())
    ready = threading.Event()
    manager = RuntimeManager(
        profiles,
        store,
        MagicMock(),
        tunnel,
        supervisor_factory=lambda name: PersistingSupervisor(store, name, ready),
    )
    services = api_services(runtime_manager=manager, profiles=profiles)

    with TestClient(create_app(services=services)) as client:
        started = client.post("/runtime/shared-profile/connect")
        assert started.status_code == 200
        assert started.json()["state"] in {"STARTING", "READY"}
        assert ready.wait(1.0)

        fresh_manager = RuntimeManager(profiles, ProfileStore(tmp_path), MagicMock(), tunnel)
        observed = fresh_manager.status("shared-profile")
        assert observed.state is SupervisorState.UNSUPERVISED
        assert observed.runtime_present is True
        assert observed.process_alive is True
        assert observed.tunnel_id == "tunnel-phase26"

        stopped = client.post("/runtime/shared-profile/disconnect")
        assert stopped.status_code == 200
        assert stopped.json()["state"] == "STOPPED"

        after = fresh_manager.status("shared-profile")
        assert after.state is SupervisorState.UNSUPERVISED
        assert after.runtime_present is False
        assert store.load_runtime("shared-profile") is None


def test_create_app_state_dir_reaches_shared_composition_root(tmp_path):
    services = api_services()
    application = create_app(tmp_path)

    with patch("app.api.app.create_services", return_value=services) as factory:
        with TestClient(application):
            pass

    factory.assert_called_once_with(tmp_path)
