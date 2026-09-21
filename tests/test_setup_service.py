from pathlib import Path

import pytest

from app.domain.errors import RABError
from app.domain.ssh_bootstrap import AuthorizedKeyInstall, HostKeyInfo, SSHKeyPair
from app.services.setup_service import SetupService


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
