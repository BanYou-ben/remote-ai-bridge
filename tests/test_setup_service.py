from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.domain.errors import RABError
from app.domain.profile import Profile
from app.domain.ssh_bootstrap import AuthorizedKeyInstall, HostKeyInfo, SSHKeyPair
from app.infrastructure.profile_store import ProfileStore
from app.services.local_proxy import LocalProxyCandidate, LocalProxyDiscovery, LocalProxyService
from app.services.profile_service import ProfileService
from app.services.remote_port import RemotePortSelector
from app.services.setup_service import SetupService
from app.services.tunnel import TunnelManager


INFO = HostKeyInfo(
    "server.example",
    22,
    "ssh-ed25519",
    "SHA256:c2VydmVyLWZpbmdlcnByaW50",
    "c2VydmVyLWtleQ==",
)
PAIR = SSHKeyPair(
    "1" * 32,
    "C:/rab/keys/private",
    "C:/rab/keys/private.pub",
    "ssh-ed25519 YWJjZA== remote-ai-bridge:" + "1" * 32,
    True,
)


class Session:
    def __init__(self, host="server.example", port=22):
        self.host_key_info = HostKeyInfo(
            host,
            port,
            INFO.key_type,
            INFO.fingerprint,
            INFO.key_base64,
        )
        self.closed = False

    def close(self):
        self.closed = True


class SFTP:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class HostKeys:
    def __init__(self, status="unknown"):
        self.current_status = status
        self.path = Path("C:/rab/known_hosts")
        self.confirmed = []
        self.removed = []
        self.remove_result = True

    def status(self, info):
        return self.current_status

    def confirm(self, info, *, accepted):
        if not accepted:
            raise RABError("HOST_KEY_REJECTED", "host key rejected")
        self.confirmed.append(info)
        self.current_status = "confirmed"
        return True

    def remove_if_matches(self, info):
        self.removed.append(info)
        return self.remove_result


class Keys:
    def __init__(self, pair=PAIR, error=None):
        self.pair = pair
        self.error = error
        self.removed = []
        self.requested_key_ids = []

    def ensure_key(self, key_id=None):
        self.requested_key_ids.append(key_id)
        if self.error:
            raise self.error
        if key_id is not None and key_id != self.pair.key_id:
            raise RABError("SSH_KEY_NOT_FOUND", "missing")
        return self.pair

    def remove_generated(self, pair):
        self.removed.append(pair)


class Adapter:
    def __init__(self):
        self.sessions = []
        self.auth_calls = []
        self.sftp = SFTP()
        self.auth_error = None
        self.install_error = None
        self.install_result = True
        self.verify_error = None
        self.removed_public_keys = []
        self.remove_result = True
        self.install_calls = []

    @staticmethod
    def _validate_username(username):
        if username != "tester":
            raise RABError("SSH_TARGET_INVALID", "bad username")

    def handshake(self, host, port):
        session = Session(host, port)
        self.sessions.append(session)
        return session

    def authenticate_password(self, session, username, password):
        self.auth_calls.append((username, password))
        if self.auth_error:
            raise self.auth_error

    def open_sftp(self, session):
        return self.sftp

    def install_public_key(self, sftp, public_key):
        self.install_calls.append(public_key)
        if self.install_error:
            raise self.install_error
        return AuthorizedKeyInstall(self.install_result)

    def verify_batch_login(self, host, port, username, pair, known_hosts):
        if self.verify_error:
            raise self.verify_error

    def rollback_public_key(self, sftp, public_key, receipt):
        self.removed_public_keys.append(public_key)
        return self.remove_result


def make_service(status="unknown", pair=PAIR):
    hosts = HostKeys(status)
    keys = Keys(pair)
    bootstrap = Adapter()
    return SetupService(hosts, keys, bootstrap), hosts, keys, bootstrap


def test_prepare_unknown_host_returns_fingerprint_without_password_auth():
    service, _, _, bootstrap = make_service()

    with pytest.raises(RABError) as raised:
        service.prepare("server.example", "tester")

    assert raised.value.code == "HOST_KEY_CONFIRMATION_REQUIRED"
    assert raised.value.retryable is False
    assert raised.value.details["fingerprint"] == INFO.fingerprint
    assert bootstrap.auth_calls == []


def test_prepare_confirmed_host_is_ready_to_auth():
    service, _, _, bootstrap = make_service("confirmed")

    result = service.prepare("server.example", "tester")

    assert result.status == "READY_TO_AUTH"
    assert result.fingerprint == INFO.fingerprint
    assert bootstrap.auth_calls == []


def test_rejected_fingerprint_never_authenticates():
    service, _, _, bootstrap = make_service()

    with pytest.raises(RABError) as raised:
        service.confirm_host_key("server.example", 22, INFO.fingerprint, accepted=False)

    assert raised.value.code == "HOST_KEY_REJECTED"
    assert bootstrap.auth_calls == []


def test_confirmation_rehandshakes_and_rejects_changed_fingerprint():
    service, hosts, _, bootstrap = make_service()

    with pytest.raises(RABError) as raised:
        service.confirm_host_key("server.example", 22, "SHA256:old", accepted=True)

    assert raised.value.code == "HOST_KEY_CHANGED"
    assert hosts.confirmed == []
    assert bootstrap.auth_calls == []


def test_bootstrap_installs_key_and_requires_batchmode_success():
    service, hosts, keys, bootstrap = make_service()

    result = service.bootstrap(
        "server.example",
        "tester",
        "temporary-password",
        confirmed_fingerprint=INFO.fingerprint,
    )

    assert result.key_id == PAIR.key_id
    assert result.public_key_added is True
    assert result.batch_login_verified is True
    assert hosts.confirmed == [INFO]
    assert keys.removed == []
    assert bootstrap.auth_calls == [("tester", "temporary-password")]
    assert keys.requested_key_ids == [None]


def test_existing_key_is_reused_only_when_caller_explicitly_supplies_key_id():
    reused = SSHKeyPair(PAIR.key_id, PAIR.private_key_path, PAIR.public_key_path, PAIR.public_key, False)
    service, _, keys, _ = make_service("confirmed", reused)

    result = service.bootstrap(
        "server.example",
        "tester",
        "temporary-password",
        existing_key_id=reused.key_id,
    )

    assert result.key_id == reused.key_id
    assert keys.requested_key_ids == [reused.key_id]


def test_two_new_managed_server_setups_receive_distinct_keys():
    class NewKeyPerSetup:
        def __init__(self):
            self.counter = 0
            self.removed = []

        def ensure_key(self, key_id=None):
            assert key_id is None
            self.counter += 1
            generated_id = f"{self.counter:032x}"
            return SSHKeyPair(
                generated_id,
                f"C:/rab/keys/id_ed25519_{generated_id}",
                f"C:/rab/keys/id_ed25519_{generated_id}.pub",
                f"ssh-ed25519 YWJjZA== remote-ai-bridge:{generated_id}",
                True,
            )

        def remove_generated(self, pair):
            self.removed.append(pair)

    hosts = HostKeys("confirmed")
    keys = NewKeyPerSetup()
    bootstrap = Adapter()
    service = SetupService(hosts, keys, bootstrap)

    server_a = service.bootstrap("server-a.example", "tester", "temporary-password")
    server_b = service.bootstrap("server-b.example", "tester", "temporary-password")

    assert server_a.key_id != server_b.key_id
    assert keys.counter == 2


def test_failure_after_known_hosts_write_rolls_back_only_new_host_entry():
    service, hosts, keys, bootstrap = make_service()
    bootstrap.auth_error = RABError("PASSWORD_AUTH_FAILED", "failed", retryable=True)

    with pytest.raises(RABError) as raised:
        service.bootstrap(
            "server.example", "tester", "bad-password", confirmed_fingerprint=INFO.fingerprint
        )

    assert raised.value.code == "PASSWORD_AUTH_FAILED"
    assert hosts.removed == [INFO]
    assert keys.removed == []
    assert bootstrap.removed_public_keys == []


def test_failure_before_public_key_write_rolls_back_generated_nothing(tmp_path):
    service, hosts, keys, bootstrap = make_service()
    keys.error = RABError("SSH_KEY_GENERATION_FAILED", "failed")

    with pytest.raises(RABError) as raised:
        service.bootstrap(
            "server.example", "tester", "temporary-password", confirmed_fingerprint=INFO.fingerprint
        )

    assert raised.value.code == "SSH_KEY_GENERATION_FAILED"
    assert hosts.removed == [INFO]
    assert bootstrap.removed_public_keys == []


def test_batch_failure_removes_only_public_key_and_local_key_created_by_setup():
    service, hosts, keys, bootstrap = make_service()
    bootstrap.verify_error = RABError("BATCH_LOGIN_FAILED", "failed")

    with pytest.raises(RABError) as raised:
        service.bootstrap(
            "server.example", "tester", "temporary-password", confirmed_fingerprint=INFO.fingerprint
        )

    assert raised.value.code == "BATCH_LOGIN_FAILED"
    assert bootstrap.removed_public_keys == [PAIR.public_key]
    assert keys.removed == [PAIR]
    assert hosts.removed == [INFO]


def test_preexisting_artifacts_are_preserved_on_failure():
    reused = SSHKeyPair(PAIR.key_id, PAIR.private_key_path, PAIR.public_key_path, PAIR.public_key, False)
    service, hosts, keys, bootstrap = make_service("confirmed", reused)
    bootstrap.install_result = False
    bootstrap.verify_error = RABError("BATCH_LOGIN_FAILED", "failed")

    with pytest.raises(RABError):
        service.bootstrap("server.example", "tester", "temporary-password")

    assert hosts.removed == []
    assert keys.removed == []
    assert bootstrap.removed_public_keys == []


def test_failed_owned_artifact_rollback_has_explicit_error():
    service, hosts, _, bootstrap = make_service()
    bootstrap.verify_error = RABError("BATCH_LOGIN_FAILED", "failed")
    bootstrap.remove_result = False

    with pytest.raises(RABError) as raised:
        service.bootstrap(
            "server.example", "tester", "temporary-password", confirmed_fingerprint=INFO.fingerprint
        )

    assert raised.value.code == "SETUP_ROLLBACK_FAILED"
    assert raised.value.details["original_error"] == "BATCH_LOGIN_FAILED"
    assert hosts.removed == [INFO]


def test_password_never_appears_in_service_error_or_details():
    secret = "unique-bootstrap-password"
    service, _, _, bootstrap = make_service()
    bootstrap.auth_error = RABError(
        "PASSWORD_AUTH_FAILED",
        f"password={secret}",
        details={"bootstrap_password": secret, "auth_token": secret},
    )

    with pytest.raises(RABError) as raised:
        service.bootstrap("server.example", "tester", secret, confirmed_fingerprint=INFO.fingerprint)

    rendered = repr(raised.value.to_dict())
    assert secret not in rendered
    assert raised.value.details["bootstrap_password"] == "[REDACTED]"
    assert raised.value.details["auth_token"] == "[REDACTED]"


def test_password_is_not_written_to_logs(caplog):
    secret = "never-log-this-bootstrap-password"
    service, _, _, bootstrap = make_service()
    bootstrap.auth_error = RABError("PASSWORD_AUTH_FAILED", "authentication failed", retryable=True)

    with pytest.raises(RABError):
        service.bootstrap("server.example", "tester", secret, confirmed_fingerprint=INFO.fingerprint)

    assert secret not in caplog.text


class ProxyDiscovery:
    def __init__(self, *, ports=(7897,), fail_on_call=None):
        self.ports = ports
        self.fail_on_call = fail_on_call
        self.calls = []

    @staticmethod
    def validate_discovery_inputs(endpoint_probe_url, **kwargs):
        return LocalProxyService.validate_discovery_inputs(endpoint_probe_url, **kwargs)

    def discover(self, endpoint_probe_url, **kwargs):
        self.calls.append((endpoint_probe_url, kwargs))
        if self.fail_on_call == len(self.calls):
            raise RABError("LOCAL_PROXY_NOT_FOUND", "proxy disappeared", retryable=True)
        selected_port = kwargs.get("selected_port")
        if selected_port is None and len(self.ports) > 1:
            raise RABError(
                "LOCAL_PROXY_SELECTION_REQUIRED",
                "select proxy",
                retryable=True,
                details={
                    "candidates": [
                        {"host": "127.0.0.1", "port": value} for value in self.ports
                    ]
                },
            )
        port = selected_port if selected_port is not None else self.ports[0]
        candidate = LocalProxyCandidate("127.0.0.1", port, True, True, True)
        return LocalProxyDiscovery(candidate, (candidate,))


class RemotePorts:
    def __init__(self, selected=17890, error=None):
        self.selected = selected
        self.error = error
        self.calls = []

    @staticmethod
    def validate_scan_parameters(**kwargs):
        return RemotePortSelector.validate_scan_parameters(**kwargs)

    def select(self, profile, **kwargs):
        self.calls.append((profile, kwargs))
        if self.error:
            raise self.error
        return self.selected


class Profiles:
    def __init__(self, error=None, preflight_error=None):
        self.error = error
        self.preflight_error = preflight_error
        self.created = []
        self.preflight_names = []

    def ensure_create_available(self, name):
        self.preflight_names.append(name)
        if self.preflight_error:
            raise self.preflight_error

    def create(self, profile):
        if self.error:
            raise self.error
        self.created.append(profile)
        return profile


def managed_setup_service(*, pair=PAIR, proxy=None, remote_ports=None, profiles=None):
    hosts = HostKeys("confirmed")
    keys = Keys(pair)
    adapter = Adapter()
    proxy = proxy or ProxyDiscovery()
    remote_ports = remote_ports or RemotePorts()
    profiles = profiles or Profiles()
    service = SetupService(
        hosts,
        keys,
        adapter,
        local_proxy=proxy,
        remote_ports=remote_ports,
        profiles=profiles,
    )
    return service, hosts, keys, adapter, proxy, remote_ports, profiles


def test_managed_setup_bootstrap_discovers_and_creates_complete_profile():
    service, _, keys, adapter, proxy, remote_ports, profiles = managed_setup_service(
        remote_ports=RemotePorts(17892)
    )

    result = service.setup_managed_profile(
        "managed-server",
        "server.example",
        "tester",
        "temporary-password",
        selected_local_proxy_port=7897,
        candidate_proxy_ports=(7890, 7897),
    )

    assert result == profiles.created[0]
    assert result == Profile(
        schema_version=2,
        name="managed-server",
        ssh_target="tester@server.example",
        local_proxy_port=7897,
        remote_port=17892,
        profile_type="managed",
        host="server.example",
        username="tester",
        ssh_port=22,
        key_id=PAIR.key_id,
        host_key_type=INFO.key_type,
        host_key_fingerprint=INFO.fingerprint,
    )
    assert len(proxy.calls) == 2
    assert remote_ports.calls[0][0].remote_bind_host == "127.0.0.1"
    assert keys.requested_key_ids == [None]
    assert adapter.install_calls == [PAIR.public_key]


def test_managed_setup_multiple_proxy_stops_before_bootstrap_side_effects():
    proxy = ProxyDiscovery(ports=(7890, 7897))
    service, _, keys, adapter, _, remote_ports, profiles = managed_setup_service(proxy=proxy)

    with pytest.raises(RABError) as raised:
        service.setup_managed_profile("managed-server", "server.example", "tester", "password")

    assert raised.value.code == "LOCAL_PROXY_SELECTION_REQUIRED"
    assert keys.requested_key_ids == []
    assert adapter.auth_calls == []
    assert remote_ports.calls == []
    assert profiles.created == []


def test_managed_setup_explicit_proxy_selection_continues_without_duplicate_bootstrap():
    proxy = ProxyDiscovery(ports=(7890, 7897))
    service, _, keys, adapter, _, _, _ = managed_setup_service(proxy=proxy)

    with pytest.raises(RABError):
        service.setup_managed_profile("managed-server", "server.example", "tester", "password")
    result = service.setup_managed_profile(
        "managed-server",
        "server.example",
        "tester",
        "password",
        selected_local_proxy_port=7897,
    )

    assert result.local_proxy_port == 7897
    assert keys.requested_key_ids == [None]
    assert len(adapter.auth_calls) == 1
    assert len(adapter.install_calls) == 1


def test_managed_setup_proxy_change_after_bootstrap_rolls_back_new_owned_key():
    proxy = ProxyDiscovery(fail_on_call=2)
    service, hosts, keys, adapter, _, _, profiles = managed_setup_service(proxy=proxy)

    with pytest.raises(RABError) as raised:
        service.setup_managed_profile("managed-server", "server.example", "tester", "password")

    assert raised.value.code == "LOCAL_PROXY_NOT_FOUND"
    assert keys.removed == [PAIR]
    assert adapter.removed_public_keys == [PAIR.public_key]
    assert profiles.created == []
    assert hosts.removed == []


def test_managed_setup_port_failure_rolls_back_new_owned_key():
    remote_ports = RemotePorts(
        error=RABError("REMOTE_PORT_RANGE_EXHAUSTED", "none free", retryable=True)
    )
    service, _, keys, adapter, _, _, profiles = managed_setup_service(remote_ports=remote_ports)

    with pytest.raises(RABError) as raised:
        service.setup_managed_profile("managed-server", "server.example", "tester", "password")

    assert raised.value.code == "REMOTE_PORT_RANGE_EXHAUSTED"
    assert keys.removed == [PAIR]
    assert adapter.removed_public_keys == [PAIR.public_key]
    assert profiles.created == []


def test_managed_setup_failure_never_deletes_explicitly_reused_key():
    reused = SSHKeyPair(PAIR.key_id, PAIR.private_key_path, PAIR.public_key_path, PAIR.public_key, False)
    remote_ports = RemotePorts(error=RABError("REMOTE_PORT_RANGE_EXHAUSTED", "none free"))
    service, _, keys, adapter, _, _, _ = managed_setup_service(pair=reused, remote_ports=remote_ports)
    adapter.install_result = False

    with pytest.raises(RABError):
        service.setup_managed_profile(
            "managed-server",
            "server.example",
            "tester",
            "password",
            existing_key_id=PAIR.key_id,
        )

    assert keys.requested_key_ids == [PAIR.key_id]
    assert keys.removed == []
    assert adapter.removed_public_keys == []


def test_managed_setup_profile_creation_race_rolls_back_new_owned_key(tmp_path):
    store = ProfileStore(tmp_path)
    delegate = ProfileService(store, MagicMock(spec=TunnelManager))

    class RacingProfiles:
        def ensure_create_available(self, name):
            delegate.ensure_create_available(name)

        def create(self, profile):
            competing = Profile.from_dict({**profile.to_dict(), "key_id": "2" * 32})
            delegate.create(competing)
            return delegate.create(profile)

    profiles = RacingProfiles()
    service, _, keys, adapter, _, _, _ = managed_setup_service(profiles=profiles)

    with pytest.raises(RABError) as raised:
        service.setup_managed_profile("managed-server", "server.example", "tester", "password")

    assert raised.value.code == "PROFILE_EXISTS"
    assert keys.removed == [PAIR]
    assert adapter.removed_public_keys == [PAIR.public_key]
    assert store.load_profile("managed-server").key_id == "2" * 32


def test_managed_setup_unexpected_completion_failure_is_distinct_and_rolls_back():
    profiles = Profiles(error=OSError("disk full"))
    service, _, keys, adapter, _, _, _ = managed_setup_service(profiles=profiles)

    with pytest.raises(RABError) as raised:
        service.setup_managed_profile("managed-server", "server.example", "tester", "password")

    assert raised.value.code == "MANAGED_SETUP_FAILED"
    assert "disk full" not in repr(raised.value.to_dict())
    assert keys.removed == [PAIR]
    assert adapter.removed_public_keys == [PAIR.public_key]


def test_managed_setup_passes_bounded_remote_scan_configuration():
    service, _, _, _, _, remote_ports, _ = managed_setup_service()

    service.setup_managed_profile(
        "managed-server",
        "server.example",
        "tester",
        "password",
        start_remote_port=20000,
        max_remote_port_attempts=7,
    )

    assert remote_ports.calls[0][1] == {"start_port": 20000, "max_attempts": 7}


@pytest.mark.parametrize(
    ("changes", "expected_code"),
    [
        ({"name": "bad name"}, "PROFILE_INVALID"),
        ({"host": "bad host"}, "PROFILE_INVALID"),
        ({"username": "bad user"}, "PROFILE_INVALID"),
        ({"port": 0}, "PROFILE_INVALID"),
        ({"auto_reconnect": "yes"}, "PROFILE_INVALID"),
        ({"endpoint_probe_url": "http://example.com/"}, "PROFILE_INVALID"),
        ({"candidate_proxy_ports": (0,)}, "LOCAL_PROXY_DISCOVERY_INVALID"),
        ({"start_remote_port": 70000}, "REMOTE_PORT_SELECTION_INVALID"),
        ({"max_remote_port_attempts": 0}, "REMOTE_PORT_SELECTION_INVALID"),
    ],
)
def test_managed_setup_invalid_input_has_no_bootstrap_side_effects(changes, expected_code):
    service, _, keys, adapter, proxy, remote_ports, profiles = managed_setup_service()
    values = {
        "name": "managed-server",
        "host": "server.example",
        "username": "tester",
        "password": "password",
    }
    values.update(changes)

    with pytest.raises(RABError) as raised:
        service.setup_managed_profile(**values)

    assert raised.value.code == expected_code
    assert adapter.auth_calls == []
    assert adapter.install_calls == []
    assert keys.requested_key_ids == []
    assert remote_ports.calls == []
    assert profiles.created == []
    assert proxy.calls == []


def test_managed_setup_existing_profile_preflight_has_no_bootstrap_side_effects(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(
        Profile(
            schema_version=2,
            name="managed-server",
            ssh_target="tester@server.example",
            local_proxy_port=7897,
            remote_port=17890,
            profile_type="managed",
            host="server.example",
            username="tester",
            ssh_port=22,
            key_id=PAIR.key_id,
            host_key_type=INFO.key_type,
            host_key_fingerprint=INFO.fingerprint,
        )
    )
    profiles = ProfileService(store, MagicMock(spec=TunnelManager))
    service, _, keys, adapter, proxy, remote_ports, _ = managed_setup_service(profiles=profiles)

    with pytest.raises(RABError) as raised:
        service.setup_managed_profile("managed-server", "server.example", "tester", "password")

    assert raised.value.code == "PROFILE_EXISTS"
    assert proxy.calls == []
    assert adapter.auth_calls == []
    assert adapter.install_calls == []
    assert keys.requested_key_ids == []
    assert remote_ports.calls == []


def test_reused_local_key_with_new_remote_install_rolls_back_only_remote_install():
    reused = SSHKeyPair(PAIR.key_id, PAIR.private_key_path, PAIR.public_key_path, PAIR.public_key, False)
    remote_ports = RemotePorts(error=RABError("REMOTE_PORT_RANGE_EXHAUSTED", "none free"))
    service, _, keys, adapter, _, _, _ = managed_setup_service(pair=reused, remote_ports=remote_ports)
    adapter.install_result = True

    with pytest.raises(RABError):
        service.setup_managed_profile(
            "managed-server",
            "server.example",
            "tester",
            "password",
            existing_key_id=PAIR.key_id,
        )

    assert keys.removed == []
    assert adapter.removed_public_keys == [PAIR.public_key]


def test_managed_setup_persists_through_real_profile_service_and_store(tmp_path):
    store = ProfileStore(tmp_path)
    profiles = ProfileService(store, MagicMock(spec=TunnelManager))
    service, _, _, _, _, _, _ = managed_setup_service(
        remote_ports=RemotePorts(17894),
        profiles=profiles,
    )

    created = service.setup_managed_profile(
        "managed-server",
        "server.example",
        "tester",
        "password",
        selected_local_proxy_port=7897,
    )

    loaded = store.load_profile("managed-server")
    assert loaded == created
    assert loaded.schema_version == 2
    assert loaded.profile_type == "managed"
    assert loaded.remote_port == 17894
    assert loaded.key_id == PAIR.key_id
