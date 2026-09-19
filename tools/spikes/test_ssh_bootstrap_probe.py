from __future__ import annotations

import errno
import io
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

from tools.spikes.ssh_bootstrap_probe import (
    HandshakeSession,
    HostKeyInfo,
    LocalProbeFiles,
    ProbeConfig,
    ProbeError,
    authenticate_after_confirmation,
    build_batch_verify_argv,
    check_existing_known_hosts,
    establish_handshake,
    install_public_key_for_current_user,
    prepare_local_files,
    sha256_fingerprint,
    write_independent_known_hosts,
)


class FakeHostKey:
    def get_name(self):
        return "ssh-ed25519"

    def asbytes(self):
        return b"server-host-key"

    def get_base64(self):
        return "c2VydmVyLWhvc3Qta2V5"


class FakeConnection:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("connection-close")


class FakeTransport:
    def __init__(self, connection, events, auth_error=None):
        self.connection = connection
        self.events = events
        self.auth_error = auth_error
        self.authenticated = False

    def start_client(self, timeout):
        self.events.append("handshake")

    def get_remote_server_key(self):
        self.events.append("host-key")
        return FakeHostKey()

    def auth_password(self, username, password, fallback):
        self.events.append(("auth", username, password, fallback))
        if self.auth_error:
            raise self.auth_error
        self.authenticated = True

    def is_authenticated(self):
        return self.authenticated

    def close(self):
        self.events.append("transport-close")


def config(tmp_path):
    return ProbeConfig("server.example", "tester", 22, tmp_path)


def session(events=None, auth_error=None):
    events = events if events is not None else []
    connection = FakeConnection(events)
    transport = FakeTransport(connection, events, auth_error)
    return HandshakeSession(transport, connection, FakeHostKey()), events


def test_handshake_gets_host_key_before_any_authentication(tmp_path):
    events = []
    connection = FakeConnection(events)
    transport = FakeTransport(connection, events)
    paramiko = SimpleNamespace(Transport=lambda value: transport)

    result = establish_handshake(config(tmp_path), paramiko, lambda address, timeout: connection)

    assert events == ["handshake", "host-key"]
    assert result.host_key_info.key_type == "ssh-ed25519"
    assert result.host_key_info.fingerprint == sha256_fingerprint(b"server-host-key")


def test_unconfirmed_fingerprint_never_reads_or_uses_password():
    active, events = session()
    password_reader_called = False

    def password_reader(prompt):
        nonlocal password_reader_called
        password_reader_called = True
        return "do-not-use"

    with pytest.raises(ProbeError) as raised:
        authenticate_after_confirmation(active, "tester", "wrong fingerprint", password_reader)

    assert raised.value.code == "HOST_KEY_NOT_CONFIRMED"
    assert password_reader_called is False
    assert not any(isinstance(event, tuple) and event[0] == "auth" for event in events)


def test_confirmed_fingerprint_reads_password_then_authenticates():
    active, events = session()

    authenticate_after_confirmation(
        active,
        "tester",
        active.host_key_info.fingerprint,
        lambda prompt: "temporary-password",
    )

    assert events[-1] == ("auth", "tester", "temporary-password", False)
    assert active.transport.is_authenticated()


def test_authentication_failure_never_exposes_password_in_exception():
    active, _ = session(auth_error=RuntimeError("backend failure"))
    password = "unique-password-that-must-not-leak"

    with pytest.raises(ProbeError) as raised:
        authenticate_after_confirmation(
            active,
            "tester",
            active.host_key_info.fingerprint,
            lambda prompt: password,
        )

    assert raised.value.code == "PASSWORD_AUTH_FAILED"
    assert password not in str(raised.value)
    assert password not in repr(raised.value)
    assert raised.value.__context__ is None


class MemoryRemoteFile:
    def __init__(self, sftp, path, mode):
        self.sftp = sftp
        self.path = path
        self.mode = mode
        self.buffer = io.StringIO(sftp.files.get(path, "") if "r" in mode or "a" in mode else "")
        if "a" in mode:
            self.buffer.seek(0, io.SEEK_END)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if "a" in self.mode or "w" in self.mode:
            self.sftp.files[self.path] = self.buffer.getvalue()

    def read(self):
        return self.buffer.getvalue()

    def write(self, value):
        return self.buffer.write(value)


class FakeSFTP:
    def __init__(self, existing=""):
        self.home = "/home/tester"
        self.files = {}
        self.modes = {f"{self.home}/.ssh": stat.S_IFDIR | 0o700}
        if existing is not None:
            path = f"{self.home}/.ssh/authorized_keys"
            self.files[path] = existing
            self.modes[path] = stat.S_IFREG | 0o600
        self.chmods = []

    def normalize(self, path):
        return self.home

    def lstat(self, path):
        if path not in self.modes:
            raise OSError(errno.ENOENT, "missing")
        return SimpleNamespace(st_mode=self.modes[path])

    def mkdir(self, path, mode):
        self.modes[path] = stat.S_IFDIR | mode

    def chmod(self, path, mode):
        self.chmods.append((path, mode))
        if path not in self.modes:
            self.modes[path] = stat.S_IFREG | mode

    def open(self, path, mode):
        return MemoryRemoteFile(self, path, mode)


PUBLIC_KEY = "ssh-ed25519 YWJjZA== remote-ai-bridge-bootstrap-probe"


def test_public_key_install_is_current_user_scoped_and_idempotent():
    sftp = FakeSFTP(existing="ssh-ed25519 b3RoZXI= existing\n")
    path = "/home/tester/.ssh/authorized_keys"

    assert install_public_key_for_current_user(sftp, PUBLIC_KEY) is True
    first_content = sftp.files[path]
    assert install_public_key_for_current_user(sftp, PUBLIC_KEY) is False

    assert sftp.files[path] == first_content
    assert first_content.count("YWJjZA==") == 1
    assert ("/home/tester/.ssh", 0o700) in sftp.chmods
    assert (path, 0o600) in sftp.chmods


def test_public_key_install_rejects_authorized_keys_symlink():
    sftp = FakeSFTP(existing="")
    path = "/home/tester/.ssh/authorized_keys"
    sftp.modes[path] = stat.S_IFLNK | 0o777

    with pytest.raises(ProbeError) as raised:
        install_public_key_for_current_user(sftp, PUBLIC_KEY)

    assert raised.value.code == "AUTHORIZED_KEYS_UNSAFE"


def local_files(tmp_path):
    return LocalProbeFiles(
        tmp_path / "key",
        tmp_path / "key.pub",
        tmp_path / "known_hosts",
        tmp_path / "empty_global_known_hosts",
        tmp_path / "empty_ssh_config",
    )


def test_independent_known_hosts_rejects_a_changed_key(tmp_path):
    files = local_files(tmp_path)
    first = HostKeyInfo("ssh-ed25519", "SHA256:first", "Zmlyc3Q=")
    changed = HostKeyInfo("ssh-ed25519", "SHA256:changed", "Y2hhbmdlZA==")

    write_independent_known_hosts(config(tmp_path), files, first)
    with pytest.raises(ProbeError) as raised:
        write_independent_known_hosts(config(tmp_path), files, changed)

    assert raised.value.code == "HOST_KEY_CHANGED"
    assert files.known_hosts.read_text(encoding="ascii") == "server.example ssh-ed25519 Zmlyc3Q=\n"


def test_existing_changed_host_key_is_rejected_before_authentication(tmp_path):
    files = local_files(tmp_path)
    files.known_hosts.write_text("server.example ssh-ed25519 b2xk\n", encoding="ascii")
    observed = HostKeyInfo("ssh-ed25519", "SHA256:new", "bmV3")

    with pytest.raises(ProbeError) as raised:
        check_existing_known_hosts(config(tmp_path), files.known_hosts, observed)

    assert raised.value.code == "HOST_KEY_CHANGED"


def test_key_generation_uses_argument_list_without_server_password_or_environment(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tools.spikes.ssh_bootstrap_probe.shutil.which",
        lambda name: r"C:\Windows\System32\OpenSSH\ssh-keygen.exe",
    )

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        private_path = Path(argv[argv.index("-f") + 1])
        private_path.write_text("temporary private key fixture", encoding="ascii")
        private_path.with_suffix(private_path.suffix + ".pub").write_text(PUBLIC_KEY, encoding="ascii")
        return subprocess.CompletedProcess(argv, 0, "", "")

    files = prepare_local_files(config(tmp_path), runner)

    argv, kwargs = calls[0]
    assert files.private_key.exists()
    assert "actual-server-password" not in " ".join(argv)
    assert "env" not in kwargs
    assert kwargs["shell"] is False


def test_batch_verification_is_strict_and_contains_no_password(tmp_path):
    files = local_files(tmp_path)

    argv = build_batch_verify_argv(config(tmp_path), files, r"C:\Windows\System32\OpenSSH\ssh.exe")

    command = " ".join(argv)
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "PasswordAuthentication=no" in argv
    assert "KbdInteractiveAuthentication=no" in argv
    assert f"UserKnownHostsFile={files.known_hosts}" in argv
    assert f"GlobalKnownHostsFile={files.empty_global_known_hosts}" in argv
    assert "password" not in command.lower().replace("passwordauthentication", "")
    assert argv[-2:] == ["tester@server.example", "true"]
