from pathlib import Path

import pytest

from app.domain.errors import RABError
from app.infrastructure.process_runner import ProcessResult
from app.infrastructure.ssh_key_store import KEY_COMMENT_PREFIX, SSHKeyStore


class RecordingRunner:
    def __init__(self, *, succeeds=True):
        self.calls = []
        self.succeeds = succeeds

    def run(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        if self.succeeds:
            private_path = Path(argv[argv.index("-f") + 1])
            key_id = private_path.name.removeprefix("id_ed25519_")
            private_path.write_text("PRIVATE FIXTURE", encoding="ascii")
            private_path.with_suffix(".pub").write_text(
                f"ssh-ed25519 YWJjZA== {KEY_COMMENT_PREFIX}{key_id}\n", encoding="ascii"
            )
            return ProcessResult(tuple(argv), 0, "", "")
        return ProcessResult(tuple(argv), 1, "", "generation failed", "PROCESS_EXIT_NONZERO")


def test_generate_creates_independent_rab_key_with_stable_id(tmp_path):
    runner = RecordingRunner()
    store = SSHKeyStore(tmp_path, runner, ssh_keygen="ssh-keygen.exe")

    pair = store.ensure_key()

    assert len(pair.key_id) == 32
    assert pair.generated is True
    assert pair.public_key.endswith(f"{KEY_COMMENT_PREFIX}{pair.key_id}")
    assert Path(pair.private_key_path).exists()
    assert Path(pair.public_key_path).exists()


def test_new_setup_generates_a_new_key_instead_of_implicitly_reusing_existing(tmp_path):
    runner = RecordingRunner()
    store = SSHKeyStore(tmp_path, runner, ssh_keygen="ssh-keygen.exe")
    first = store.ensure_key()

    second = store.ensure_key()

    assert second.key_id != first.key_id
    assert second.private_key_path != first.private_key_path
    assert second.generated is True
    assert len(runner.calls) == 2


def test_existing_key_is_reused_only_when_key_id_is_explicit(tmp_path):
    runner = RecordingRunner()
    store = SSHKeyStore(tmp_path, runner, ssh_keygen="ssh-keygen.exe")
    first = store.ensure_key()

    reused = store.ensure_key(first.key_id)

    assert reused.key_id == first.key_id
    assert reused.private_key_path == first.private_key_path
    assert reused.generated is False
    assert len(runner.calls) == 1


def test_generation_uses_argument_array_and_never_contains_private_material(tmp_path):
    runner = RecordingRunner()
    store = SSHKeyStore(tmp_path, runner, ssh_keygen="ssh-keygen.exe")

    pair = store.ensure_key()
    argv, _ = runner.calls[0]

    assert isinstance(argv, tuple)
    assert argv[0] == "ssh-keygen.exe"
    assert "-N" in argv and argv[argv.index("-N") + 1] == ""
    assert "PRIVATE FIXTURE" not in " ".join(argv)
    assert pair.private_key_path not in pair.public_key


def test_generation_failure_does_not_expose_process_stderr(tmp_path):
    store = SSHKeyStore(tmp_path, RecordingRunner(succeeds=False), ssh_keygen="ssh-keygen.exe")

    with pytest.raises(RABError) as raised:
        store.ensure_key()

    assert raised.value.code == "SSH_KEY_GENERATION_FAILED"
    assert "generation failed" not in str(raised.value)


def test_load_rejects_invalid_key_id_before_path_use(tmp_path):
    store = SSHKeyStore(tmp_path, RecordingRunner(), ssh_keygen="ssh-keygen.exe")

    with pytest.raises(RABError) as raised:
        store.load("../user-key")

    assert raised.value.code == "SSH_KEY_ID_INVALID"


def test_remove_generated_never_removes_reused_key(tmp_path):
    store = SSHKeyStore(tmp_path, RecordingRunner(), ssh_keygen="ssh-keygen.exe")
    generated = store.ensure_key()
    reused = store.load(generated.key_id)

    store.remove_generated(reused)

    assert Path(generated.private_key_path).exists()
    assert Path(generated.public_key_path).exists()


def test_remove_generated_refuses_key_replaced_after_creation(tmp_path):
    store = SSHKeyStore(tmp_path, RecordingRunner(), ssh_keygen="ssh-keygen.exe")
    generated = store.ensure_key()
    Path(generated.public_key_path).write_text(
        f"ssh-ed25519 b3RoZXI= {KEY_COMMENT_PREFIX}{generated.key_id}\n", encoding="ascii"
    )

    with pytest.raises(RABError) as raised:
        store.remove_generated(generated)

    assert raised.value.code == "SETUP_ROLLBACK_FAILED"
    assert Path(generated.private_key_path).exists()


def test_rollback_of_new_server_key_does_not_affect_other_profile_key(tmp_path):
    store = SSHKeyStore(tmp_path, RecordingRunner(), ssh_keygen="ssh-keygen.exe")
    server_b = store.ensure_key()
    server_a = store.ensure_key()

    store.remove_generated(server_a)

    assert not Path(server_a.private_key_path).exists()
    assert not Path(server_a.public_key_path).exists()
    assert store.load(server_b.key_id).public_key == server_b.public_key
