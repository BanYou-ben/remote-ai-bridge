from __future__ import annotations

import errno
import io
from pathlib import Path
import stat
import traceback
from types import SimpleNamespace

import pytest

from app.domain.errors import RABError
from app.domain.ssh_bootstrap import SSHKeyPair
from app.infrastructure.process_runner import ProcessResult
from app.infrastructure.ssh_bootstrap import BATCH_MARKER, SSHBootstrapAdapter


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
    def __init__(self, events, auth_error=None, authenticated=True):
        self.events = events
        self.auth_error = auth_error
        self.authenticated = authenticated

    def start_client(self, timeout):
        self.events.append("handshake")

    def get_remote_server_key(self):
        self.events.append("host-key")
        return FakeHostKey()

    def auth_password(self, username, password, fallback):
        self.events.append(("auth", username, password, fallback))
        if self.auth_error:
            raise self.auth_error

    def is_authenticated(self):
        return self.authenticated

    def close(self):
        self.events.append("transport-close")


def adapter(tmp_path, *, auth_error=None, authenticated=True, runner=None):
    events = []
    connection = FakeConnection(events)
    transport = FakeTransport(events, auth_error, authenticated)
    paramiko = SimpleNamespace(
        Transport=lambda value: transport,
        SFTPClient=SimpleNamespace(from_transport=lambda value: None),
    )
    value = SSHBootstrapAdapter(
        tmp_path,
        runner=runner,
        paramiko_module=paramiko,
        socket_factory=lambda address, timeout: connection,
        ssh_executable="ssh.exe",
    )
    return value, events


def test_handshake_observes_fingerprint_before_password_auth(tmp_path):
    value, events = adapter(tmp_path)

    session = value.handshake("server.example", 22)

    assert events == ["handshake", "host-key"]
    assert session.host_key_info.key_type == "ssh-ed25519"
    assert session.host_key_info.fingerprint.startswith("SHA256:")


def test_correct_password_uses_paramiko_without_keyboard_interactive_fallback(tmp_path):
    value, events = adapter(tmp_path)
    session = value.handshake("server.example", 22)

    value.authenticate_password(session, "tester", "temporary-password")

    assert events[-1] == ("auth", "tester", "temporary-password", False)


def test_wrong_password_returns_safe_retryable_error(tmp_path):
    secret = "unique-password-value"
    value, _ = adapter(tmp_path, auth_error=RuntimeError(f"backend included {secret}"))
    session = value.handshake("server.example", 22)

    with pytest.raises(RABError) as raised:
        value.authenticate_password(session, "tester", secret)

    assert raised.value.code == "PASSWORD_AUTH_FAILED"
    assert raised.value.retryable is True
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert raised.value.__context__ is None
    assert secret not in "".join(traceback.format_exception(raised.type, raised.value, raised.tb))


def test_partial_password_auth_is_reported_as_mfa_unsupported(tmp_path):
    PartialAuthentication = type("PartialAuthentication", (RuntimeError,), {})
    value, _ = adapter(tmp_path, auth_error=PartialAuthentication())
    session = value.handshake("server.example", 22)

    with pytest.raises(RABError) as raised:
        value.authenticate_password(session, "tester", "temporary-password")

    assert raised.value.code == "SSH_MFA_REQUIRED"


def test_password_auth_method_unsupported_is_distinct_from_wrong_password(tmp_path):
    BadAuthenticationType = type("BadAuthenticationType", (RuntimeError,), {})
    value, _ = adapter(tmp_path, auth_error=BadAuthenticationType())
    session = value.handshake("server.example", 22)

    with pytest.raises(RABError) as raised:
        value.authenticate_password(session, "tester", "temporary-password")

    assert raised.value.code == "AUTH_METHOD_UNSUPPORTED"


class MemoryRemoteFile:
    def __init__(self, sftp, path, mode):
        self.sftp = sftp
        self.path = path
        self.mode = mode
        self.buffer = io.StringIO(sftp.files.get(path, "") if "r" in mode else "")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if "w" in self.mode:
            self.sftp.files[self.path] = self.buffer.getvalue()
            self.sftp.modes[self.path] = stat.S_IFREG | 0o600

    def read(self):
        return self.buffer.getvalue()

    def write(self, value):
        return self.buffer.write(value)


class FakeSFTP:
    def __init__(self, existing=None):
        self.home = "/home/tester"
        self.files = {}
        self.modes = {f"{self.home}/.ssh": stat.S_IFDIR | 0o755}
        if existing is not None:
            path = f"{self.home}/.ssh/authorized_keys"
            self.files[path] = existing
            self.modes[path] = stat.S_IFREG | 0o644
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

    def open(self, path, mode):
        return MemoryRemoteFile(self, path, mode)

    def posix_rename(self, source, destination):
        self.files[destination] = self.files.pop(source)
        self.modes[destination] = self.modes.pop(source)

    def remove(self, path):
        self.files.pop(path, None)
        self.modes.pop(path, None)

    def listdir(self, path):
        prefix = path.rstrip("/") + "/"
        names = {item[len(prefix):].split("/", 1)[0] for item in self.modes if item.startswith(prefix)}
        return sorted(name for name in names if name)

    def rmdir(self, path):
        if self.listdir(path):
            raise OSError("directory is not empty")
        self.modes.pop(path)


PUBLIC_KEY = "ssh-ed25519 YWJjZA== remote-ai-bridge:11111111111111111111111111111111"


def test_authorized_keys_is_created_for_current_user_with_safe_permissions(tmp_path):
    value, _ = adapter(tmp_path)
    sftp = FakeSFTP(existing=None)

    receipt = value.install_public_key(sftp, PUBLIC_KEY)

    assert receipt.public_key_added is True
    assert receipt.authorized_keys_created is True
    path = "/home/tester/.ssh/authorized_keys"
    assert sftp.files[path] == PUBLIC_KEY + "\n"
    assert ("/home/tester/.ssh", 0o700) in sftp.chmods
    assert stat.S_IMODE(sftp.modes[path]) == 0o600


def test_existing_authorized_keys_is_preserved_and_duplicate_key_not_added(tmp_path):
    value, _ = adapter(tmp_path)
    existing = "ssh-ed25519 b3RoZXI= existing\n"
    sftp = FakeSFTP(existing=existing)

    assert value.install_public_key(sftp, PUBLIC_KEY).public_key_added is True
    first = sftp.files["/home/tester/.ssh/authorized_keys"]
    assert value.install_public_key(sftp, PUBLIC_KEY).public_key_added is False

    assert first.startswith(existing)
    assert first.count("YWJjZA==") == 1


def test_existing_key_with_authorized_keys_options_is_not_duplicated(tmp_path):
    value, _ = adapter(tmp_path)
    existing = 'restrict,command="true" ssh-ed25519 YWJjZA== existing-comment\n'
    sftp = FakeSFTP(existing=existing)

    receipt = value.install_public_key(sftp, PUBLIC_KEY)

    assert receipt.public_key_added is False
    assert sftp.files["/home/tester/.ssh/authorized_keys"] == existing


def test_symlink_ssh_directory_is_rejected(tmp_path):
    value, _ = adapter(tmp_path)
    sftp = FakeSFTP()
    sftp.modes["/home/tester/.ssh"] = stat.S_IFLNK | 0o777

    with pytest.raises(RABError) as raised:
        value.install_public_key(sftp, PUBLIC_KEY)

    assert raised.value.code == "AUTHORIZED_KEYS_UNSAFE"


def test_symlink_authorized_keys_is_rejected(tmp_path):
    value, _ = adapter(tmp_path)
    sftp = FakeSFTP(existing="")
    sftp.modes["/home/tester/.ssh/authorized_keys"] = stat.S_IFLNK | 0o777

    with pytest.raises(RABError) as raised:
        value.install_public_key(sftp, PUBLIC_KEY)

    assert raised.value.code == "AUTHORIZED_KEYS_UNSAFE"


def test_owned_public_key_can_be_removed_without_touching_other_entries(tmp_path):
    value, _ = adapter(tmp_path)
    other = "ssh-ed25519 b3RoZXI= existing"
    sftp = FakeSFTP(existing=f"{other}\n{PUBLIC_KEY}\n")

    assert value.remove_public_key(sftp, PUBLIC_KEY) is True

    assert sftp.files["/home/tester/.ssh/authorized_keys"] == other + "\n"


def test_rollback_removes_only_remote_files_and_directory_created_by_setup(tmp_path):
    value, _ = adapter(tmp_path)
    sftp = FakeSFTP(existing=None)
    sftp.modes.clear()

    receipt = value.install_public_key(sftp, PUBLIC_KEY)

    assert receipt.ssh_dir_created is True
    assert receipt.authorized_keys_created is True
    assert value.rollback_public_key(sftp, PUBLIC_KEY, receipt) is True
    assert "/home/tester/.ssh" not in sftp.modes
    assert "/home/tester/.ssh/authorized_keys" not in sftp.files


class BatchRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        return self.result


def key_pair(tmp_path):
    return SSHKeyPair("1" * 32, str(tmp_path / "private"), str(tmp_path / "public"), PUBLIC_KEY, False)


def test_batchmode_verification_uses_strict_windows_openssh_options(tmp_path):
    runner = BatchRunner(ProcessResult(("ssh.exe",), 0, BATCH_MARKER + "\n", ""))
    value, _ = adapter(tmp_path, runner=runner)
    known_hosts = tmp_path / "ssh" / "known_hosts"

    value.verify_batch_login("server.example", 22, "tester", key_pair(tmp_path), known_hosts)

    argv = runner.calls[0][0]
    assert "BatchMode=yes" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "PasswordAuthentication=no" in argv
    assert "KbdInteractiveAuthentication=no" in argv
    assert "IdentitiesOnly=yes" in argv
    assert argv[-1] == "echo RAB_BOOTSTRAP_OK"


def test_batchmode_failure_is_not_accepted(tmp_path):
    runner = BatchRunner(ProcessResult(("ssh.exe",), 1, "", "denied", "PROCESS_EXIT_NONZERO"))
    value, _ = adapter(tmp_path, runner=runner)

    with pytest.raises(RABError) as raised:
        value.verify_batch_login("server.example", 22, "tester", key_pair(tmp_path), tmp_path / "known_hosts")

    assert raised.value.code == "BATCH_LOGIN_FAILED"
    assert "denied" not in str(raised.value)
