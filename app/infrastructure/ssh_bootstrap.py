from __future__ import annotations

import base64
import hashlib
from pathlib import Path
import posixpath
import re
import shutil
import socket
import stat
from typing import Any, Callable
import uuid

from app.domain.errors import RABError
from app.domain.ssh_bootstrap import AuthorizedKeyInstall, HostKeyInfo, SSHKeyPair
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.profile_store import default_state_root


HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
BATCH_MARKER = "RAB_BOOTSTRAP_OK"


class BootstrapSession:
    def __init__(self, transport: Any, connection: Any, host_key: Any, info: HostKeyInfo) -> None:
        self.transport = transport
        self.connection = connection
        self.host_key = host_key
        self.host_key_info = info

    def close(self) -> None:
        try:
            self.transport.close()
        finally:
            self.connection.close()


class SSHBootstrapAdapter:
    def __init__(
        self,
        root: Path | None = None,
        *,
        runner: ProcessRunner | None = None,
        paramiko_module: Any | None = None,
        socket_factory: Callable[..., Any] = socket.create_connection,
        ssh_executable: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.root = root or default_state_root()
        self.runner = runner or ProcessRunner()
        self._paramiko = paramiko_module
        self.socket_factory = socket_factory
        self.ssh_executable = ssh_executable
        self.timeout = timeout

    def handshake(self, host: str, port: int) -> BootstrapSession:
        self._validate_target(host, port)
        paramiko = self._load_paramiko()
        connection = None
        transport = None
        try:
            connection = self.socket_factory((host, port), timeout=self.timeout)
            transport = paramiko.Transport(connection)
            transport.start_client(timeout=self.timeout)
            host_key = transport.get_remote_server_key()
            key_bytes = host_key.asbytes()
            info = HostKeyInfo(
                host,
                port,
                host_key.get_name(),
                _sha256_fingerprint(key_bytes),
                host_key.get_base64(),
            )
            return BootstrapSession(transport, connection, host_key, info)
        except Exception:
            try:
                if transport is not None:
                    transport.close()
                elif connection is not None:
                    connection.close()
            except Exception:
                pass
            raise RABError(
                "SSH_HANDSHAKE_FAILED",
                "SSH handshake failed before password authentication",
                retryable=True,
                details={"host": host, "port": port},
            ) from None

    def authenticate_password(self, session: BootstrapSession, username: str, password: str) -> None:
        self._validate_username(username)
        if not password:
            raise RABError("PASSWORD_AUTH_FAILED", "SSH password authentication failed", retryable=True)
        auth_error_code: str | None = None
        try:
            session.transport.auth_password(username, password, fallback=False)
        except Exception as exc:
            class_name = type(exc).__name__.lower()
            if "partial" in class_name:
                auth_error_code = "SSH_MFA_REQUIRED"
            elif "badauthenticationtype" in class_name:
                auth_error_code = "AUTH_METHOD_UNSUPPORTED"
            else:
                auth_error_code = "PASSWORD_AUTH_FAILED"
        finally:
            # CPython strings cannot be reliably erased. This method never persists,
            # logs, serializes, or includes the temporary password in an exception.
            password = ""
        if auth_error_code == "SSH_MFA_REQUIRED":
            raise RABError(
                "SSH_MFA_REQUIRED",
                "the SSH server requires an unsupported additional authentication method",
            )
        if auth_error_code == "AUTH_METHOD_UNSUPPORTED":
            raise RABError(
                "AUTH_METHOD_UNSUPPORTED",
                "the SSH server does not support direct password authentication",
            )
        if auth_error_code == "PASSWORD_AUTH_FAILED":
            raise RABError("PASSWORD_AUTH_FAILED", "SSH password authentication failed", retryable=True)
        if not session.transport.is_authenticated():
            raise RABError(
                "SSH_MFA_REQUIRED",
                "password authentication did not complete SSH authentication",
            )

    def open_sftp(self, session: BootstrapSession) -> Any:
        try:
            return self._load_paramiko().SFTPClient.from_transport(session.transport)
        except Exception:
            raise RABError("SSH_BOOTSTRAP_FAILED", "an SFTP session could not be opened") from None

    def install_public_key(self, sftp: Any, public_key: str) -> AuthorizedKeyInstall:
        home, ssh_dir, authorized_keys = self._safe_remote_paths(sftp)
        del home
        identity = _public_key_identity(public_key)
        if identity is None:
            raise RABError("SSH_KEY_INVALID", "the RAB public key is invalid")
        ssh_dir_created = self._ensure_ssh_directory(sftp, ssh_dir)
        try:
            existing, authorized_keys_existed = self._read_authorized_keys(sftp, authorized_keys)
        except RABError:
            if ssh_dir_created and not self._remove_empty_created_directory(sftp, ssh_dir):
                raise RABError(
                    "SETUP_ROLLBACK_FAILED",
                    "the newly created remote .ssh directory could not be safely rolled back",
                ) from None
            raise
        if any(_public_key_identity(line) == identity for line in existing.splitlines()):
            self._set_remote_permissions(sftp, ssh_dir, authorized_keys)
            return AuthorizedKeyInstall(False, ssh_dir_created, not authorized_keys_existed)
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        try:
            self._replace_authorized_keys(sftp, authorized_keys, existing + prefix + public_key + "\n")
        except RABError:
            if ssh_dir_created and not self._remove_empty_created_directory(sftp, ssh_dir):
                raise RABError(
                    "SETUP_ROLLBACK_FAILED",
                    "the newly created remote .ssh directory could not be safely rolled back",
                ) from None
            raise
        return AuthorizedKeyInstall(True, ssh_dir_created, not authorized_keys_existed)

    def remove_public_key(self, sftp: Any, public_key: str) -> bool:
        _, ssh_dir, authorized_keys = self._safe_remote_paths(sftp)
        self._ensure_ssh_directory(sftp, ssh_dir)
        existing, _ = self._read_authorized_keys(sftp, authorized_keys)
        lines = existing.splitlines()
        exact = public_key.strip()
        try:
            index = lines.index(exact)
        except ValueError:
            return False
        del lines[index]
        updated = "\n".join(lines)
        if updated:
            updated += "\n"
        self._replace_authorized_keys(sftp, authorized_keys, updated)
        return True

    def rollback_public_key(
        self,
        sftp: Any,
        public_key: str,
        receipt: AuthorizedKeyInstall,
    ) -> bool:
        _, ssh_dir, authorized_keys = self._safe_remote_paths(sftp)
        if receipt.public_key_added and not self.remove_public_key(sftp, public_key):
            return False
        if receipt.authorized_keys_created:
            content, exists = self._read_authorized_keys(sftp, authorized_keys)
            if exists:
                if content:
                    return False
                try:
                    sftp.remove(authorized_keys)
                except Exception:
                    return False
        if receipt.ssh_dir_created:
            try:
                if sftp.listdir(ssh_dir):
                    return False
                sftp.rmdir(ssh_dir)
            except Exception:
                return False
        return True

    def verify_batch_login(
        self,
        host: str,
        port: int,
        username: str,
        pair: SSHKeyPair,
        known_hosts: Path,
    ) -> None:
        self._validate_target(host, port)
        self._validate_username(username)
        executable = self.ssh_executable or shutil.which("ssh.exe") or shutil.which("ssh")
        if executable is None:
            raise RABError("BATCH_LOGIN_FAILED", "Windows ssh.exe was not found")
        config_dir = self.root / "ssh"
        config_dir.mkdir(parents=True, exist_ok=True)
        empty_config = config_dir / "empty_config"
        empty_global_hosts = config_dir / "empty_global_known_hosts"
        for path in (empty_config, empty_global_hosts):
            if not path.exists():
                path.touch()
        argv = self.build_batch_argv(
            executable,
            host,
            port,
            username,
            pair.private_key_path,
            known_hosts,
            empty_config,
            empty_global_hosts,
        )
        result = self.runner.run(argv, self.timeout)
        if not result.ok or result.stdout.strip() != BATCH_MARKER:
            raise RABError(
                "BATCH_LOGIN_FAILED",
                "Windows OpenSSH rejected the managed passwordless login",
                retryable=True,
                details={"host": host, "port": port, "username": username},
            )

    @staticmethod
    def build_batch_argv(
        executable: str,
        host: str,
        port: int,
        username: str,
        private_key_path: str,
        known_hosts: Path,
        empty_config: Path,
        empty_global_hosts: Path,
    ) -> list[str]:
        return [
            executable,
            "-T",
            "-F",
            str(empty_config),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            f"GlobalKnownHostsFile={empty_global_hosts}",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-i",
            private_key_path,
            "-p",
            str(port),
            f"{username}@{host}",
            "echo RAB_BOOTSTRAP_OK",
        ]

    def _safe_remote_paths(self, sftp: Any) -> tuple[str, str, str]:
        try:
            home = posixpath.normpath(sftp.normalize("."))
        except Exception:
            raise RABError("AUTHORIZED_KEYS_UNSAFE", "the authenticated user's home could not be resolved") from None
        if not home.startswith("/") or home == "/":
            raise RABError("AUTHORIZED_KEYS_UNSAFE", "the authenticated user's home is unsafe")
        ssh_dir = posixpath.join(home, ".ssh")
        return home, ssh_dir, posixpath.join(ssh_dir, "authorized_keys")

    @staticmethod
    def _ensure_ssh_directory(sftp: Any, ssh_dir: str) -> bool:
        try:
            attributes = sftp.lstat(ssh_dir)
        except OSError as exc:
            if getattr(exc, "errno", None) != 2:
                raise RABError("AUTHORIZED_KEYS_UNSAFE", "the remote .ssh directory could not be inspected") from None
            try:
                sftp.mkdir(ssh_dir, 0o700)
                sftp.chmod(ssh_dir, 0o700)
            except Exception:
                raise RABError("AUTHORIZED_KEYS_WRITE_FAILED", "the remote .ssh directory could not be created") from None
            return True
        if stat.S_ISLNK(attributes.st_mode) or not stat.S_ISDIR(attributes.st_mode):
            raise RABError("AUTHORIZED_KEYS_UNSAFE", "the remote .ssh path is not a regular directory")
        try:
            sftp.chmod(ssh_dir, 0o700)
        except Exception:
            raise RABError("AUTHORIZED_KEYS_WRITE_FAILED", "the remote .ssh permissions could not be set") from None
        return False

    @staticmethod
    def _read_authorized_keys(sftp: Any, path: str) -> tuple[str, bool]:
        try:
            attributes = sftp.lstat(path)
        except OSError as exc:
            if getattr(exc, "errno", None) == 2:
                return "", False
            raise RABError("AUTHORIZED_KEYS_UNSAFE", "authorized_keys could not be inspected") from None
        if stat.S_ISLNK(attributes.st_mode) or not stat.S_ISREG(attributes.st_mode):
            raise RABError("AUTHORIZED_KEYS_UNSAFE", "authorized_keys is not a regular file")
        try:
            with sftp.open(path, "r") as handle:
                value = handle.read()
        except Exception:
            raise RABError("AUTHORIZED_KEYS_WRITE_FAILED", "authorized_keys could not be read") from None
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8"), True
            except UnicodeDecodeError:
                raise RABError("AUTHORIZED_KEYS_UNSAFE", "authorized_keys is not valid UTF-8") from None
        return str(value), True

    @staticmethod
    def _replace_authorized_keys(sftp: Any, path: str, content: str) -> None:
        temporary = f"{path}.rab-{uuid.uuid4().hex}.tmp"
        try:
            with sftp.open(temporary, "w") as handle:
                handle.write(content)
            sftp.chmod(temporary, 0o600)
            if hasattr(sftp, "posix_rename"):
                sftp.posix_rename(temporary, path)
            else:
                sftp.rename(temporary, path)
        except Exception:
            try:
                sftp.remove(temporary)
            except Exception:
                pass
            raise RABError("AUTHORIZED_KEYS_WRITE_FAILED", "authorized_keys could not be updated") from None

    @staticmethod
    def _set_remote_permissions(sftp: Any, ssh_dir: str, authorized_keys: str) -> None:
        try:
            sftp.chmod(ssh_dir, 0o700)
            sftp.chmod(authorized_keys, 0o600)
        except Exception:
            raise RABError("AUTHORIZED_KEYS_WRITE_FAILED", "SSH user file permissions could not be set") from None

    @staticmethod
    def _remove_empty_created_directory(sftp: Any, path: str) -> bool:
        try:
            if sftp.listdir(path):
                return False
            sftp.rmdir(path)
            return True
        except Exception:
            return False

    def _load_paramiko(self) -> Any:
        if self._paramiko is not None:
            return self._paramiko
        try:
            import paramiko
        except ImportError:
            raise RABError("SSH_BOOTSTRAP_UNAVAILABLE", "Paramiko 5.0.0 is required for SSH bootstrap") from None
        self._paramiko = paramiko
        return paramiko

    @staticmethod
    def _validate_target(host: str, port: int) -> None:
        if not HOST_RE.fullmatch(host):
            raise RABError("SSH_TARGET_INVALID", "SSH host is invalid")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise RABError("SSH_TARGET_INVALID", "SSH port must be from 1 to 65535")

    @staticmethod
    def _validate_username(username: str) -> None:
        if not USERNAME_RE.fullmatch(username):
            raise RABError("SSH_TARGET_INVALID", "SSH username is invalid")


def _sha256_fingerprint(value: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(value).digest()).decode("ascii").rstrip("=")


def _public_key_identity(value: str) -> tuple[str, str] | None:
    parts = value.strip().split()
    key_index = next((index for index, part in enumerate(parts) if part.startswith("ssh-")), None)
    if key_index is None or key_index + 1 >= len(parts):
        return None
    key_type = parts[key_index]
    key_body = parts[key_index + 1]
    try:
        base64.b64decode(key_body.encode("ascii"), validate=True)
    except Exception:
        return None
    return key_type, key_body
