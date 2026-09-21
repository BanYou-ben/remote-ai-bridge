import pytest

from app.domain.profile import Profile, ProfileValidationError, RuntimeState
from app.infrastructure.profile_store import ProfileLockError, ProfileNotFoundError, ProfileStore


def valid_profile(**changes):
    values = {
        "schema_version": 1,
        "name": "myserver",
        "ssh_target": "user@myserver",
        "local_proxy_port": 7897,
        "remote_port": 17890,
    }
    values.update(changes)
    return Profile(**values)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "::1"])
def test_remote_bind_must_be_ipv4_loopback(host):
    with pytest.raises(ProfileValidationError):
        valid_profile(remote_bind_host=host).validate()


@pytest.mark.parametrize("target", ["-oProxyCommand=bad", "host;calc", "host name", "$(bad)"])
def test_ssh_target_rejects_option_and_shell_metacharacters(target):
    with pytest.raises(ProfileValidationError):
        valid_profile(ssh_target=target).validate()


@pytest.mark.parametrize("port", [0, 65536, -1, True])
def test_ports_are_validated(port):
    with pytest.raises(ProfileValidationError):
        valid_profile(remote_port=port).validate()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1/models",
        "https://user:secret@example.com/a",
        "https://api.openai.com/v1/models?api_key=secret",
        "not-a-url",
    ],
)
def test_probe_url_requires_credential_free_https(url):
    with pytest.raises(ProfileValidationError):
        valid_profile(endpoint_probe_url=url).validate()


def test_profiles_and_runtime_are_stored_separately(tmp_path):
    store = ProfileStore(tmp_path)
    profile = valid_profile()
    state = RuntimeState(
        1,
        profile.name,
        123,
        1000.5,
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        "tunnel-1",
        99,
        profile.remote_port,
        "2026-09-02T00:00:00+00:00",
    )
    store.save_profile(profile)
    store.save_runtime(state)

    assert store.load_profile(profile.name) == profile
    assert store.load_runtime(profile.name) == state
    assert (tmp_path / "profiles" / "myserver.json").exists()
    assert (tmp_path / "runtime" / "myserver.json").exists()


def test_v1_profile_schema_loads_and_future_schema_is_rejected_explicitly():
    data = valid_profile().to_dict()

    assert Profile.from_dict(data) == valid_profile()

    data["schema_version"] = 3
    with pytest.raises(ProfileValidationError, match="unsupported profile schema_version"):
        Profile.from_dict(data)


def test_managed_v2_profile_round_trip_and_contains_no_private_material():
    profile = Profile(
        schema_version=2,
        name="managed-server",
        ssh_target="tester@server.example",
        profile_type="managed",
        host="server.example",
        username="tester",
        ssh_port=2222,
        key_id="1" * 32,
        host_key_type="ssh-ed25519",
        host_key_fingerprint="SHA256:YWJjZGVmZ2hpamtsbW5vcHFyc3Q",
    )

    payload = profile.to_dict()

    assert Profile.from_dict(payload) == profile
    assert payload["profile_type"] == "managed"
    assert "password" not in payload
    assert "private_key" not in payload
    assert "private_key_path" not in payload


@pytest.mark.parametrize(
    "key_type",
    [
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
    ],
)
def test_managed_profile_accepts_valid_host_key_algorithm_tokens(key_type):
    profile = Profile(
        schema_version=2,
        name="managed-server",
        ssh_target="tester@server.example",
        profile_type="managed",
        host="server.example",
        username="tester",
        ssh_port=22,
        key_id="1" * 32,
        host_key_type=key_type,
        host_key_fingerprint="SHA256:YWJjZGVmZ2hpamtsbW5vcHFyc3Q",
    )

    profile.validate()


@pytest.mark.parametrize("key_type", ["", "ssh ed25519", "ssh-ed25519\nmalicious"])
def test_managed_profile_rejects_unsafe_host_key_algorithm_tokens(key_type):
    profile = Profile(
        schema_version=2,
        name="managed-server",
        ssh_target="tester@server.example",
        profile_type="managed",
        host="server.example",
        username="tester",
        ssh_port=22,
        key_id="1" * 32,
        host_key_type=key_type,
        host_key_fingerprint="SHA256:YWJjZGVmZ2hpamtsbW5vcHFyc3Q",
    )

    with pytest.raises(ProfileValidationError, match="host key type"):
        profile.validate()


@pytest.mark.parametrize("field", ["password", "ssh_password", "private_key", "private_key_content"])
def test_managed_profile_rejects_secret_fields_during_deserialization(field):
    payload = {
        "schema_version": 2,
        "name": "managed-server",
        "ssh_target": "tester@server.example",
        "profile_type": "managed",
        "host": "server.example",
        "username": "tester",
        "ssh_port": 22,
        "key_id": "1" * 32,
        "host_key_type": "ssh-ed25519",
        "host_key_fingerprint": "SHA256:YWJjZGVmZ2hpamtsbW5vcHFyc3Q",
        field: "must-not-persist",
    }

    with pytest.raises(ProfileValidationError, match="invalid profile fields"):
        Profile.from_dict(payload)


def test_runtime_rejects_partial_remote_identity():
    state = RuntimeState(
        1,
        "myserver",
        123,
        1000.5,
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        "11111111-1111-4111-8111-111111111111",
        99,
        17890,
        "2026-09-02T00:00:00+00:00",
        remote_user="tester",
    )

    with pytest.raises(ProfileValidationError, match="incomplete remote tunnel identity"):
        state.validate()


def test_runtime_migrates_obsolete_listener_pid_without_serializing_it():
    data = {
        "schema_version": 1,
        "profile_name": "myserver",
        "pid": 123,
        "process_creation_time": 1000.5,
        "executable_path": r"C:\Windows\System32\OpenSSH\ssh.exe",
        "tunnel_id": "11111111-1111-4111-8111-111111111111",
        "supervisor_pid": 99,
        "remote_port": 17890,
        "started_at": "2026-09-02T00:00:00+00:00",
        "remote_user": "tester",
        "remote_sshd_pid": 690949,
        "remote_listener_pid": 690949,
        "remote_sshd_start_ticks": 123456,
        "remote_boot_id": "11111111-2222-4333-8444-555555555555",
        "remote_ssh_connection": "192.0.2.10 50000 192.0.2.20 22",
    }

    migrated = RuntimeState.from_dict(data)

    assert migrated.has_remote_identity
    assert "remote_listener_pid" not in migrated.to_dict()


def test_runtime_cannot_serialize_bootstrap_password_or_private_key_material():
    state = RuntimeState(
        1,
        "myserver",
        123,
        1000.5,
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        "tunnel-1",
        99,
        17890,
        "2026-09-02T00:00:00+00:00",
    )
    payload = state.to_dict()

    assert "password" not in payload
    assert "private_key" not in payload
    with pytest.raises(ProfileValidationError, match="invalid runtime fields"):
        RuntimeState.from_dict({**payload, "bootstrap_password": "must-not-persist"})


def test_store_refuses_path_traversal(tmp_path):
    store = ProfileStore(tmp_path)
    with pytest.raises(ValueError):
        store.load_profile("../outside")


def test_only_one_foreground_supervisor_lock_per_profile(tmp_path):
    store = ProfileStore(tmp_path)
    with store.supervisor_lock("myserver"):
        with pytest.raises(ProfileLockError):
            with store.supervisor_lock("myserver"):
                pass

    with store.supervisor_lock("myserver"):
        pass


def test_store_update_uses_validated_atomic_writer(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path)
    original = valid_profile()
    store.save_profile(original)
    updated = valid_profile(local_proxy_port=7890)
    writes = []
    real_write = store._write_json

    def recording_write(path, data):
        writes.append((path, data))
        real_write(path, data)

    monkeypatch.setattr(store, "_write_json", recording_write)

    store.update_profile(updated)

    assert store.load_profile("myserver") == updated
    assert writes == [(tmp_path / "profiles" / "myserver.json", updated.to_dict())]


def test_store_update_refuses_to_create_missing_profile(tmp_path):
    store = ProfileStore(tmp_path)

    with pytest.raises(ProfileNotFoundError, match="profile not found"):
        store.update_profile(valid_profile())


def test_store_delete_keeps_runtime_separate(tmp_path):
    store = ProfileStore(tmp_path)
    profile = valid_profile()
    state = RuntimeState(
        1,
        profile.name,
        123,
        1000.5,
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        "tunnel-1",
        99,
        profile.remote_port,
        "2026-09-02T00:00:00+00:00",
    )
    store.save_profile(profile)
    store.save_runtime(state)

    store.delete_profile(profile.name)

    with pytest.raises(ProfileNotFoundError, match="profile not found"):
        store.load_profile(profile.name)
    assert store.load_runtime(profile.name) == state
