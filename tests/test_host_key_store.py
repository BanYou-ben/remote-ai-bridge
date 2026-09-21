import pytest

from app.domain.errors import RABError
from app.domain.ssh_bootstrap import HostKeyInfo
from app.infrastructure.host_key_store import HostKeyStore


def host_key(body="c2VydmVyLWtleQ==", fingerprint="SHA256:server"):
    return HostKeyInfo("server.example", 22, "ssh-ed25519", fingerprint, body)


def test_unknown_host_requires_confirmation_without_writing(tmp_path):
    store = HostKeyStore(tmp_path)

    assert store.status(host_key()) == "unknown"
    assert not store.path.exists()


def test_confirmed_host_is_written_and_recognized(tmp_path):
    store = HostKeyStore(tmp_path)

    assert store.confirm(host_key(), accepted=True) is True
    assert store.status(host_key()) == "confirmed"
    assert store.confirm(host_key(), accepted=True) is False
    assert store.path.read_text(encoding="ascii").count("server.example") == 1


def test_changed_host_key_is_rejected_without_overwrite(tmp_path):
    store = HostKeyStore(tmp_path)
    store.confirm(host_key(), accepted=True)
    original = store.path.read_text(encoding="ascii")

    with pytest.raises(RABError) as raised:
        store.status(host_key("Y2hhbmdlZA==", "SHA256:changed"))

    assert raised.value.code == "HOST_KEY_CHANGED"
    assert store.path.read_text(encoding="ascii") == original


def test_rejected_host_key_is_not_written(tmp_path):
    store = HostKeyStore(tmp_path)

    with pytest.raises(RABError) as raised:
        store.confirm(host_key(), accepted=False)

    assert raised.value.code == "HOST_KEY_REJECTED"
    assert not store.path.exists()


def test_remove_if_matches_only_removes_exact_owned_entry(tmp_path):
    store = HostKeyStore(tmp_path)
    first = host_key()
    second = HostKeyInfo("other.example", 2222, "ssh-ed25519", "SHA256:other", "b3RoZXI=")
    store.confirm(first, accepted=True)
    store.confirm(second, accepted=True)

    assert store.remove_if_matches(first) is True
    assert store.status(second) == "confirmed"
    assert "other.example" in store.path.read_text(encoding="ascii")


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
def test_valid_openssh_host_key_algorithms_are_accepted(tmp_path, key_type):
    store = HostKeyStore(tmp_path)
    observed = HostKeyInfo("server.example", 22, key_type, "SHA256:test", "c2VydmVyLWtleQ==")

    assert store.confirm(observed, accepted=True) is True
    assert store.status(observed) == "confirmed"


@pytest.mark.parametrize("key_type", ["", "ssh ed25519", "ssh-ed25519\nmalicious"])
def test_invalid_host_key_algorithm_tokens_are_rejected(tmp_path, key_type):
    store = HostKeyStore(tmp_path)
    observed = HostKeyInfo("server.example", 22, key_type, "SHA256:test", "c2VydmVyLWtleQ==")

    with pytest.raises(RABError) as raised:
        store.status(observed)

    assert raised.value.code == "HOST_KEY_INVALID"
    assert not store.path.exists()
