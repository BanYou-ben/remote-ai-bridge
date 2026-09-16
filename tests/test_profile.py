import pytest

from app.domain.profile import Profile, ProfileValidationError, RuntimeState
from app.infrastructure.profile_store import ProfileLockError, ProfileStore


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
