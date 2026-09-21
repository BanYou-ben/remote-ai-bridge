from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.domain.errors import RABError
from app.domain.ssh_bootstrap import BootstrapResult, HostKeyInfo, HostPreparation, SSHKeyPair
from app.infrastructure.ssh_key_store import SSHKeyStore
from tools.validation.phase2_2_bootstrap_validation import VALIDATION_MARKER, build_parser, run


FINGERPRINT = "SHA256:c2VydmVyLWZpbmdlcnByaW50"
KEY_ID = "1" * 32
PUBLIC_KEY = f"ssh-ed25519 YWJjZA== remote-ai-bridge:{KEY_ID}"


class FakeSetup:
    def __init__(self, *, unknown=False):
        self.unknown = unknown
        self.prepare_calls = []
        self.confirm_calls = []
        self.bootstrap_calls = []

    def prepare(self, host, username, port):
        self.prepare_calls.append((host, username, port))
        if self.unknown:
            raise RABError(
                "HOST_KEY_CONFIRMATION_REQUIRED",
                "confirmation required",
                details={
                    "host": host,
                    "port": port,
                    "key_type": "ssh-ed25519",
                    "fingerprint": FINGERPRINT,
                },
            )
        return HostPreparation(host, port, "ssh-ed25519", FINGERPRINT, "READY_TO_AUTH")

    def confirm_host_key(self, host, port, fingerprint, *, accepted):
        self.confirm_calls.append((host, port, fingerprint, accepted))
        return HostPreparation(host, port, "ssh-ed25519", fingerprint, "READY_TO_AUTH")

    def bootstrap(self, host, username, password, **kwargs):
        self.bootstrap_calls.append((host, username, password, kwargs))
        return BootstrapResult(host, kwargs["port"], username, KEY_ID, "ssh-ed25519", FINGERPRINT, True)


class Unused:
    pass


def factory_for(setup, observed_roots):
    def factory(root):
        observed_roots.append(root)
        return setup, Unused(), Unused(), Unused()

    return factory


def common_args(command, state_root):
    return [
        command,
        "--host",
        "server.example",
        "--username",
        "tester",
        "--port",
        "22",
        "--state-root",
        str(state_root),
    ]


def test_wrapper_has_no_password_argv_option(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(common_args("bootstrap", tmp_path) + ["--password", "secret"])


def test_prepare_uses_independent_state_root_and_stops_for_confirmation(tmp_path, capsys):
    setup = FakeSetup(unknown=True)
    observed_roots = []
    state_root = tmp_path / "rab-phase2-2-formal-validation"

    result = run(common_args("prepare", state_root), service_factory=factory_for(setup, observed_roots))

    output = capsys.readouterr().out
    assert result == 3
    assert observed_roots == [state_root.resolve()]
    assert (state_root / VALIDATION_MARKER).is_file()
    assert FINGERPRINT in output
    assert "ssh-ed25519" in output
    assert setup.bootstrap_calls == []


def test_bootstrap_reads_password_locally_without_outputting_secret(tmp_path, capsys):
    setup = FakeSetup()
    secret = "temporary-password-must-not-appear"
    argv = common_args("bootstrap", tmp_path / "validation") + [
        "--confirm-fingerprint",
        FINGERPRINT,
    ]

    result = run(
        argv,
        password_reader=lambda prompt: secret,
        service_factory=factory_for(setup, []),
    )

    captured = capsys.readouterr()
    assert result == 0
    assert secret not in captured.out
    assert secret not in captured.err
    assert "private_key" not in captured.out
    assert KEY_ID in captured.out
    assert setup.confirm_calls == [("server.example", 22, FINGERPRINT, True)]


def test_existing_key_id_is_forwarded_explicitly_for_reuse(tmp_path):
    setup = FakeSetup()
    argv = common_args("bootstrap", tmp_path / "validation") + [
        "--confirm-fingerprint",
        FINGERPRINT,
        "--existing-key-id",
        KEY_ID,
    ]

    assert run(argv, password_reader=lambda prompt: "secret", service_factory=factory_for(setup, [])) == 0

    assert setup.bootstrap_calls[0][3]["existing_key_id"] == KEY_ID


def test_wrapper_does_not_reimplement_ssh_protocol_operations():
    source_path = Path(__file__).with_name("phase2_2_bootstrap_validation.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(item.name.split(".", 1)[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    defined_functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}

    assert imported_roots.isdisjoint({"paramiko", "socket", "subprocess"})
    assert defined_functions.isdisjoint(
        {
            "auth_password",
            "install_public_key",
            "verify_batch_login",
            "handshake",
            "generate_key",
        }
    )


def test_normal_rab_state_root_is_rejected(monkeypatch, tmp_path, capsys):
    normal_root = tmp_path / "normal-rab-state"
    monkeypatch.setattr(
        "tools.validation.phase2_2_bootstrap_validation.default_state_root",
        lambda: normal_root,
    )

    result = run(common_args("prepare", normal_root), service_factory=factory_for(FakeSetup(), []))

    assert result == 2
    assert "VALIDATION_STATE_ROOT_UNSAFE" in capsys.readouterr().err


def test_cleanup_delegates_exact_public_key_removal_and_emits_no_password(tmp_path, capsys):
    pair = SSHKeyPair(KEY_ID, "private", "public", PUBLIC_KEY, False)

    class HostKeys:
        def __init__(self):
            self.removed = []

        def status(self, info):
            return "confirmed"

        def remove_if_matches(self, info):
            self.removed.append(info)
            return True

    class Keys:
        def __init__(self):
            self.removed = []

        def load(self, key_id):
            assert key_id == KEY_ID
            return pair

        def remove_key(self, key_id, expected_public_key):
            self.removed.append((key_id, expected_public_key))

    class Session:
        host_key_info = HostKeyInfo(
            "server.example", 22, "ssh-ed25519", FINGERPRINT, "c2VydmVyLWtleQ=="
        )

        def close(self):
            pass

    class SFTP:
        def close(self):
            pass

    class Adapter:
        def __init__(self):
            self.auth = []
            self.removed = []

        def handshake(self, host, port):
            return Session()

        def authenticate_password(self, session, username, password):
            self.auth.append((username, password))

        def open_sftp(self, session):
            return SFTP()

        def remove_public_key(self, sftp, public_key):
            self.removed.append(public_key)
            return True

    state_root = tmp_path / "validation"
    state_root.mkdir()
    (state_root / VALIDATION_MARKER).touch()
    hosts = HostKeys()
    keys = Keys()
    adapter = Adapter()
    secret = "cleanup-password-must-not-appear"

    result = run(
        common_args("cleanup", state_root) + ["--key-id", KEY_ID],
        password_reader=lambda prompt: secret,
        service_factory=lambda root: (Unused(), hosts, keys, adapter),
    )

    captured = capsys.readouterr()
    assert result == 0
    assert secret not in captured.out
    assert secret not in captured.err
    assert adapter.removed == [PUBLIC_KEY]
    assert keys.removed == [(KEY_ID, PUBLIC_KEY)]
    assert len(hosts.removed) == 1


def test_local_validation_key_removal_requires_exact_public_identity(tmp_path):
    store = SSHKeyStore(tmp_path)
    keys_dir = tmp_path / "ssh" / "keys"
    keys_dir.mkdir(parents=True)
    private_key = keys_dir / f"id_ed25519_{KEY_ID}"
    public_key = private_key.with_suffix(".pub")
    private_key.write_text("private-fixture", encoding="ascii")
    public_key.write_text(PUBLIC_KEY + "\n", encoding="ascii")

    with pytest.raises(RABError) as raised:
        store.remove_key(KEY_ID, "ssh-ed25519 b3RoZXI= remote-ai-bridge:other")

    assert raised.value.code == "SSH_KEY_IDENTITY_MISMATCH"
    assert private_key.exists()
    store.remove_key(KEY_ID, PUBLIC_KEY)
    assert not private_key.exists()
    assert not public_key.exists()
