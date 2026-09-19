from unittest.mock import MagicMock

import pytest

from app.domain.errors import RABError
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile, RuntimeState
from app.infrastructure.process_identity import ProcessInspector
from app.infrastructure.profile_store import ProfileStore
from app.services.profile_service import ProfileService
from app.services.tunnel import TunnelManager


SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"


def profile(**changes):
    values = {
        "schema_version": 1,
        "name": "server",
        "ssh_target": "user@server",
        "local_proxy_port": 7897,
        "remote_port": 17890,
    }
    values.update(changes)
    return Profile(**values)


def runtime():
    return RuntimeState(
        1,
        "server",
        4321,
        1234.5,
        SSH,
        "11111111-1111-4111-8111-111111111111",
        111,
        17890,
        "2026-09-02T00:00:00+00:00",
    )


def runtime_with_remote_identity():
    return RuntimeState(
        **{
            **runtime().to_dict(),
            "remote_user": "tester",
            "remote_sshd_pid": 690949,
            "remote_sshd_start_ticks": 123456,
            "remote_boot_id": "11111111-2222-4333-8444-555555555555",
            "remote_ssh_connection": "192.0.2.10 50000 192.0.2.20 22",
        }
    )


def service(tmp_path, tunnel=None):
    store = ProfileStore(tmp_path)
    return ProfileService(store, tunnel or MagicMock()), store


def test_profile_service_create_list_and_get(tmp_path):
    profiles, _ = service(tmp_path)

    created = profiles.create(profile())

    assert created == profile()
    assert profiles.list() == [profile()]
    assert profiles.get("server") == profile()


def test_profile_service_update_success(tmp_path):
    profiles, _ = service(tmp_path)
    profiles.create(profile())

    updated = profiles.update("server", {"local_proxy_port": 7890, "auto_reconnect": False})

    assert updated.local_proxy_port == 7890
    assert updated.auto_reconnect is False
    assert profiles.get("server") == updated


@pytest.mark.parametrize("field", ["name", "schema_version", "remote_bind_host", "unknown"])
def test_profile_service_update_rejects_invalid_field(tmp_path, field):
    profiles, _ = service(tmp_path)
    profiles.create(profile())

    with pytest.raises(RABError) as raised:
        profiles.update("server", {field: "unsafe"})

    assert raised.value.code == "PROFILE_FIELD_NOT_UPDATABLE"
    assert profiles.get("server") == profile()


def test_profile_service_update_validates_new_values(tmp_path):
    profiles, _ = service(tmp_path)
    profiles.create(profile())

    with pytest.raises(RABError) as raised:
        profiles.update("server", {"remote_port": 0})

    assert raised.value.code == "PROFILE_INVALID"
    assert profiles.get("server") == profile()


def test_profile_service_update_is_rejected_while_supervisor_lock_is_held(tmp_path):
    profiles, store = service(tmp_path)
    profiles.create(profile())

    with store.supervisor_lock("server"):
        with pytest.raises(RABError) as raised:
            profiles.update("server", {"local_proxy_port": 7890})

    assert raised.value.code == "PROFILE_BUSY"
    assert profiles.get("server") == profile()


def test_profile_service_update_rejects_existing_runtime_without_termination(tmp_path):
    tunnel = MagicMock()
    profiles, store = service(tmp_path, tunnel)
    profiles.create(profile())
    state = runtime()
    store.save_runtime(state)

    with pytest.raises(RABError) as raised:
        profiles.update("server", {"remote_port": 17891})

    assert raised.value.code == "PROFILE_RUNTIME_ACTIVE"
    assert raised.value.retryable is True
    assert profiles.get("server") == profile()
    assert store.load_runtime("server") == state
    tunnel.disconnect.assert_not_called()
    tunnel.inspector.terminate_owned.assert_not_called()


def test_profile_service_deletes_disconnected_profile(tmp_path):
    profiles, _ = service(tmp_path)
    profiles.create(profile())

    result = profiles.delete("server")

    assert result.name == "server"
    assert profiles.list() == []


def test_profile_service_delete_is_rejected_while_supervisor_lock_is_held(tmp_path):
    tunnel = MagicMock()
    profiles, store = service(tmp_path, tunnel)
    profiles.create(profile())

    with store.supervisor_lock("server"):
        with pytest.raises(RABError) as raised:
            profiles.delete("server")

    assert raised.value.code == "PROFILE_BUSY"
    tunnel.disconnect.assert_not_called()
    assert profiles.get("server") == profile()


def test_profile_service_delete_stops_owned_orphan_process(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    store.save_runtime(runtime_with_remote_identity())
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = True
    inspector.terminate_owned.return_value = True
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult(
        "Remote listener", CheckStatus.FAIL, "absent", "LISTENER_ABSENT"
    )
    tunnel = TunnelManager(MagicMock(), inspector, store, remote, SSH)
    profiles = ProfileService(store, tunnel)

    result = profiles.delete("server")

    assert result.stopped_owned_process is True
    inspector.terminate_owned.assert_called_once()
    remote.check_listener.assert_called_once_with(profile())
    remote.terminate_verified_stale_session.assert_not_called()
    assert store.load_runtime("server") is None
    assert profiles.list() == []


def test_profile_service_delete_identity_mismatch_never_terminates(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    store.save_runtime(runtime())
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = False
    tunnel = TunnelManager(MagicMock(), inspector, store, MagicMock(), SSH)
    profiles = ProfileService(store, tunnel)

    result = profiles.delete("server")

    assert result.removed_stale_runtime is True
    inspector.terminate_owned.assert_not_called()
    assert store.load_runtime("server") is None
    assert profiles.list() == []


def test_profile_service_delete_preserves_remote_ownership_when_local_identity_mismatches(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    state = runtime_with_remote_identity()
    store.save_runtime(state)
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = False
    tunnel = TunnelManager(MagicMock(), inspector, store, MagicMock(), SSH)
    profiles = ProfileService(store, tunnel)

    with pytest.raises(RABError) as raised:
        profiles.delete("server")

    assert raised.value.code == "PROFILE_DELETE_REMOTE_STATE_UNRESOLVED"
    assert raised.value.retryable is True
    assert profiles.get("server") == profile()
    preserved = store.load_runtime("server")
    assert preserved == state
    assert preserved.has_remote_identity
    assert preserved.remote_identity() == state.remote_identity()
    inspector.terminate_owned.assert_not_called()


def test_profile_service_delete_preserves_remote_evidence_if_process_exits_during_stop(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    state = runtime_with_remote_identity()
    store.save_runtime(state)
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.side_effect = [True, False]
    inspector.terminate_owned.return_value = False
    remote = MagicMock()
    tunnel = TunnelManager(MagicMock(), inspector, store, remote, SSH)
    profiles = ProfileService(store, tunnel)

    with pytest.raises(RABError) as raised:
        profiles.delete("server")

    assert raised.value.code == "PROFILE_DELETE_REMOTE_STATE_UNRESOLVED"
    assert profiles.get("server") == profile()
    assert store.load_runtime("server") == state
    inspector.terminate_owned.assert_called_once_with(state, tunnel.stop_timeout)
    remote.check_listener.assert_not_called()


def test_profile_service_delete_preserves_remote_identity_when_listener_remains(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    state = runtime_with_remote_identity()
    store.save_runtime(state)
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = True
    inspector.terminate_owned.return_value = True
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult(
        "Remote listener", CheckStatus.PASS, "127.0.0.1:17890 is listening"
    )
    tunnel = TunnelManager(MagicMock(), inspector, store, remote, SSH)
    profiles = ProfileService(store, tunnel)

    with pytest.raises(RABError) as raised:
        profiles.delete("server")

    assert raised.value.code == "PROFILE_DELETE_REMOTE_STATE_UNRESOLVED"
    assert raised.value.retryable is True
    assert profiles.get("server") == profile()
    preserved = store.load_runtime("server")
    assert preserved == state
    assert preserved.remote_identity() == state.remote_identity()
    inspector.terminate_owned.assert_called_once_with(state, tunnel.stop_timeout)
    remote.terminate_verified_stale_session.assert_not_called()


def test_profile_service_delete_preserves_remote_identity_when_listener_check_fails(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    state = runtime_with_remote_identity()
    store.save_runtime(state)
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = True
    inspector.terminate_owned.return_value = True
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult(
        "Remote listener", CheckStatus.FAIL, "SSH transport failed", "REMOTE_CHECK_FAILED"
    )
    tunnel = TunnelManager(MagicMock(), inspector, store, remote, SSH)
    profiles = ProfileService(store, tunnel)

    with pytest.raises(RABError) as raised:
        profiles.delete("server")

    assert raised.value.code == "PROFILE_DELETE_REMOTE_STATE_UNRESOLVED"
    assert profiles.get("server") == profile()
    assert store.load_runtime("server") == state
    remote.terminate_verified_stale_session.assert_not_called()


def test_profile_service_delete_unknown_process_never_terminates(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    store.save_runtime(runtime())
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = False
    inspector.get_identity.return_value = None
    tunnel = TunnelManager(MagicMock(), inspector, store, MagicMock(), SSH)
    profiles = ProfileService(store, tunnel)

    profiles.delete("server")

    inspector.terminate_owned.assert_not_called()
    assert store.load_runtime("server") is None


def test_profile_service_delete_keeps_profile_when_owned_process_will_not_stop(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    store.save_runtime(runtime())
    tunnel = MagicMock()
    tunnel.disconnect.return_value = CheckResult(
        "Tunnel process", CheckStatus.FAIL, "owned process did not exit", "PROCESS_STOP_TIMEOUT"
    )
    profiles = ProfileService(store, tunnel)

    with pytest.raises(RABError) as raised:
        profiles.delete("server")

    assert raised.value.code == "PROFILE_DELETE_TUNNEL_STOP_FAILED"
    assert profiles.get("server") == profile()
    assert store.load_runtime("server") == runtime()
