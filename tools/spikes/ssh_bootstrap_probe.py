"""Isolated SSH bootstrap feasibility probe.

This is not production RAB code. It deliberately keeps Paramiko out of the
project dependencies and uses it only for the one-time password-authenticated
bootstrap experiment. The final login verification is always Windows OpenSSH.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import getpass
import hashlib
import os
from pathlib import Path
import posixpath
import re
import shutil
import socket
import stat
import subprocess
import sys
from typing import Any, Callable


KEY_COMMENT = "remote-ai-bridge-bootstrap-probe"
KEY_BASENAME = "id_ed25519_rab_bootstrap_probe"
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


class ProbeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def __repr__(self) -> str:
        return f"ProbeError(code={self.code!r}, message={self.message!r})"


@dataclass(frozen=True)
class ProbeConfig:
    host: str
    username: str
    port: int
    work_dir: Path
    timeout: float = 15.0

    def validate(self) -> None:
        if not HOST_RE.fullmatch(self.host):
            raise ProbeError("INVALID_HOST", "host must be a DNS name or IPv4 address without whitespace")
        if not USERNAME_RE.fullmatch(self.username):
            raise ProbeError("INVALID_USERNAME", "username contains unsupported characters")
        if isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise ProbeError("INVALID_PORT", "port must be from 1 to 65535")
        if self.timeout <= 0:
            raise ProbeError("INVALID_TIMEOUT", "timeout must be positive")


@dataclass(frozen=True)
class HostKeyInfo:
    key_type: str
    fingerprint: str
    key_base64: str


@dataclass(frozen=True)
class LocalProbeFiles:
    private_key: Path
    public_key: Path
    known_hosts: Path
    empty_global_known_hosts: Path
    empty_ssh_config: Path


class HandshakeSession:
    def __init__(self, transport: Any, connection: Any, host_key: Any) -> None:
        self.transport = transport
        self.connection = connection
        self.host_key = host_key
        self.host_key_info = HostKeyInfo(
            key_type=host_key.get_name(),
            fingerprint=sha256_fingerprint(host_key.asbytes()),
            key_base64=host_key.get_base64(),
        )

    def close(self) -> None:
        try:
            self.transport.close()
        finally:
            self.connection.close()


def sha256_fingerprint(key_bytes: bytes) -> str:
    digest = hashlib.sha256(key_bytes).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def establish_handshake(
    config: ProbeConfig,
    paramiko_module: Any,
    socket_factory: Callable[..., Any] = socket.create_connection,
) -> HandshakeSession:
    """Perform SSH negotiation and obtain the host key, without authenticating."""
    config.validate()
    try:
        connection = socket_factory((config.host, config.port), timeout=config.timeout)
        transport = paramiko_module.Transport(connection)
        transport.start_client(timeout=config.timeout)
        host_key = transport.get_remote_server_key()
        return HandshakeSession(transport, connection, host_key)
    except Exception:
        try:
            connection.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        raise ProbeError("SSH_HANDSHAKE_FAILED", "SSH handshake failed before authentication") from None


def authenticate_after_confirmation(
    session: HandshakeSession,
    username: str,
    confirmed_fingerprint: str,
    password_reader: Callable[[str], str] = getpass.getpass,
) -> None:
    """Read and use a password only after an exact fingerprint confirmation."""
    if confirmed_fingerprint.strip() != session.host_key_info.fingerprint:
        raise ProbeError("HOST_KEY_NOT_CONFIRMED", "host key fingerprint was not confirmed")

    password: str | None = password_reader("SSH password (input hidden): ")
    try:
        if not password:
            raise ProbeError("PASSWORD_REQUIRED", "a password is required for the bootstrap probe")
        authentication_failed = False
        try:
            # fallback=False prevents an implicit keyboard-interactive/MFA path.
            session.transport.auth_password(username, password, fallback=False)
        except Exception:
            authentication_failed = True
        if authentication_failed:
            # Raise outside the except block so the safe error has no captured
            # third-party exception context that could be rendered by a caller.
            raise ProbeError("PASSWORD_AUTH_FAILED", "password authentication failed")
        if not session.transport.is_authenticated():
            raise ProbeError(
                "MULTI_FACTOR_OR_ADDITIONAL_AUTH_REQUIRED",
                "password authentication did not complete authentication",
            )
    finally:
        # Python strings cannot be reliably zeroed. Drop the final local reference
        # immediately; the value is never copied into argv, env, files, or logs.
        password = None


def prepare_local_files(
    config: ProbeConfig,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> LocalProbeFiles:
    config.work_dir.mkdir(parents=True, exist_ok=True)
    files = local_probe_files(config.work_dir)
    private_key = files.private_key
    public_key = files.public_key

    if private_key.exists() != public_key.exists():
        raise ProbeError("LOCAL_KEY_INCOMPLETE", "temporary probe key pair is incomplete")
    if not private_key.exists():
        ssh_keygen = shutil.which("ssh-keygen.exe") or shutil.which("ssh-keygen")
        if ssh_keygen is None:
            raise ProbeError("SSH_KEYGEN_NOT_FOUND", "Windows ssh-keygen was not found on PATH")
        argv = [
            ssh_keygen,
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            KEY_COMMENT,
            "-f",
            str(private_key),
        ]
        try:
            result = runner(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=config.timeout,
                check=False,
            )
        except Exception:
            raise ProbeError("SSH_KEYGEN_FAILED", "could not run Windows ssh-keygen") from None
        if result.returncode != 0 or not private_key.exists() or not public_key.exists():
            raise ProbeError("SSH_KEYGEN_FAILED", "Windows ssh-keygen did not create the probe key pair")

    files.empty_global_known_hosts.touch(exist_ok=True)
    files.empty_ssh_config.touch(exist_ok=True)
    return files


def local_probe_files(work_dir: Path) -> LocalProbeFiles:
    return LocalProbeFiles(
        work_dir / KEY_BASENAME,
        work_dir / f"{KEY_BASENAME}.pub",
        work_dir / "known_hosts",
        work_dir / "empty_global_known_hosts",
        work_dir / "empty_ssh_config",
    )


def read_public_key(public_key_path: Path) -> str:
    try:
        value = public_key_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        raise ProbeError("PUBLIC_KEY_READ_FAILED", "could not read the generated public key") from None
    parts = value.split()
    if len(parts) < 2 or not parts[0].startswith("ssh-"):
        raise ProbeError("PUBLIC_KEY_INVALID", "generated public key has an invalid format")
    try:
        base64.b64decode(parts[1].encode("ascii"), validate=True)
    except Exception:
        raise ProbeError("PUBLIC_KEY_INVALID", "generated public key data is invalid") from None
    return f"{parts[0]} {parts[1]} {KEY_COMMENT}"


def install_public_key_for_current_user(sftp: Any, public_key: str) -> bool:
    """Install one key in the authenticated user's home. Return True if added."""
    try:
        home = posixpath.normpath(sftp.normalize("."))
    except Exception:
        raise ProbeError("REMOTE_HOME_UNAVAILABLE", "could not resolve the authenticated user's home") from None
    if not home.startswith("/") or home == "/":
        raise ProbeError("REMOTE_HOME_UNSAFE", "authenticated user home path is unsafe")

    ssh_dir = posixpath.join(home, ".ssh")
    authorized_keys = posixpath.join(ssh_dir, "authorized_keys")
    _ensure_remote_directory(sftp, ssh_dir)
    existing = _read_authorized_keys(sftp, authorized_keys)

    new_identity = _public_key_identity(public_key)
    for line in existing.splitlines():
        if _public_key_identity(line) == new_identity:
            sftp.chmod(ssh_dir, 0o700)
            sftp.chmod(authorized_keys, 0o600)
            return False

    prefix = "" if not existing or existing.endswith("\n") else "\n"
    try:
        with sftp.open(authorized_keys, "a") as handle:
            handle.write(prefix + public_key + "\n")
        sftp.chmod(ssh_dir, 0o700)
        sftp.chmod(authorized_keys, 0o600)
    except Exception:
        raise ProbeError("PUBLIC_KEY_INSTALL_FAILED", "could not update the current user's authorized_keys") from None
    return True


def _ensure_remote_directory(sftp: Any, path: str) -> None:
    try:
        attributes = sftp.lstat(path)
    except OSError as exc:
        if not _is_missing(exc):
            raise ProbeError("REMOTE_SSH_DIRECTORY_UNAVAILABLE", "could not inspect the current user's .ssh") from None
        try:
            sftp.mkdir(path, 0o700)
            sftp.chmod(path, 0o700)
        except Exception:
            raise ProbeError("REMOTE_SSH_DIRECTORY_CREATE_FAILED", "could not create the current user's .ssh") from None
        return
    if stat.S_ISLNK(attributes.st_mode) or not stat.S_ISDIR(attributes.st_mode):
        raise ProbeError("REMOTE_SSH_DIRECTORY_UNSAFE", "the current user's .ssh is not a regular directory")
    sftp.chmod(path, 0o700)


def _read_authorized_keys(sftp: Any, path: str) -> str:
    try:
        attributes = sftp.lstat(path)
    except OSError as exc:
        if _is_missing(exc):
            return ""
        raise ProbeError("AUTHORIZED_KEYS_UNAVAILABLE", "could not inspect authorized_keys") from None
    if stat.S_ISLNK(attributes.st_mode) or not stat.S_ISREG(attributes.st_mode):
        raise ProbeError("AUTHORIZED_KEYS_UNSAFE", "authorized_keys is not a regular file")
    try:
        with sftp.open(path, "r") as handle:
            value = handle.read()
    except Exception:
        raise ProbeError("AUTHORIZED_KEYS_UNAVAILABLE", "could not read authorized_keys") from None
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise ProbeError("AUTHORIZED_KEYS_INVALID", "authorized_keys is not valid UTF-8") from None
    return str(value)


def _public_key_identity(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split()
    if len(parts) < 2 or not parts[0].startswith("ssh-"):
        return None
    return parts[0], parts[1]


def _is_missing(exc: OSError) -> bool:
    return getattr(exc, "errno", None) == 2


def write_independent_known_hosts(config: ProbeConfig, files: LocalProbeFiles, host_key: HostKeyInfo) -> None:
    host_token = config.host if config.port == 22 else f"[{config.host}]:{config.port}"
    line = f"{host_token} {host_key.key_type} {host_key.key_base64}\n"
    if files.known_hosts.exists():
        try:
            existing = files.known_hosts.read_text(encoding="ascii")
        except (OSError, UnicodeError):
            raise ProbeError("KNOWN_HOSTS_UNAVAILABLE", "could not read the independent known_hosts file") from None
        if existing != line:
            raise ProbeError("HOST_KEY_CHANGED", "independent known_hosts contains a different host key")
        return
    temporary = files.known_hosts.with_suffix(".tmp")
    try:
        temporary.write_text(line, encoding="ascii")
        os.replace(temporary, files.known_hosts)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise ProbeError("KNOWN_HOSTS_WRITE_FAILED", "could not write the independent known_hosts file") from None


def check_existing_known_hosts(config: ProbeConfig, known_hosts: Path, host_key: HostKeyInfo) -> None:
    if not known_hosts.exists():
        return
    host_token = config.host if config.port == 22 else f"[{config.host}]:{config.port}"
    expected = f"{host_token} {host_key.key_type} {host_key.key_base64}\n"
    try:
        existing = known_hosts.read_text(encoding="ascii")
    except (OSError, UnicodeError):
        raise ProbeError("KNOWN_HOSTS_UNAVAILABLE", "could not read the independent known_hosts file") from None
    if existing != expected:
        raise ProbeError("HOST_KEY_CHANGED", "independent known_hosts contains a different host key")


def build_batch_verify_argv(config: ProbeConfig, files: LocalProbeFiles, ssh_executable: str) -> list[str]:
    return [
        ssh_executable,
        "-T",
        "-F",
        str(files.empty_ssh_config),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={files.known_hosts}",
        "-o",
        f"GlobalKnownHostsFile={files.empty_global_known_hosts}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        f"ConnectTimeout={max(1, int(config.timeout))}",
        "-i",
        str(files.private_key),
        "-p",
        str(config.port),
        f"{config.username}@{config.host}",
        "true",
    ]


def verify_batch_login(
    config: ProbeConfig,
    files: LocalProbeFiles,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    ssh_executable = shutil.which("ssh.exe") or shutil.which("ssh")
    if ssh_executable is None:
        raise ProbeError("SSH_NOT_FOUND", "Windows ssh.exe was not found on PATH")
    argv = build_batch_verify_argv(config, files, ssh_executable)
    try:
        result = runner(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=config.timeout,
            check=False,
        )
    except Exception:
        raise ProbeError("BATCH_LOGIN_FAILED", "Windows OpenSSH batch login could not be executed") from None
    if result.returncode != 0:
        raise ProbeError("BATCH_LOGIN_FAILED", "Windows OpenSSH rejected the passwordless batch login")


def run_probe(config: ProbeConfig, paramiko_module: Any) -> None:
    session = establish_handshake(config, paramiko_module)
    try:
        info = session.host_key_info
        print(f"Host: {config.host}:{config.port}")
        print(f"Host key type: {info.key_type}")
        print(f"Fingerprint: {info.fingerprint}")
        files = local_probe_files(config.work_dir)
        check_existing_known_hosts(config, files.known_hosts, info)
        confirmation = input("Type the exact fingerprint to trust this host, or press Enter to cancel: ")
        authenticate_after_confirmation(session, config.username, confirmation)

        files = prepare_local_files(config)
        public_key = read_public_key(files.public_key)
        try:
            sftp = paramiko_module.SFTPClient.from_transport(session.transport)
            added = install_public_key_for_current_user(sftp, public_key)
        except ProbeError:
            raise
        except Exception:
            raise ProbeError("SFTP_FAILED", "could not open the authenticated user's SFTP session") from None
        finally:
            try:
                sftp.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass

        write_independent_known_hosts(config, files, info)
        session.close()
        verify_batch_login(config, files)
        print("Password authentication: PASS")
        print(f"Public key installation: {'ADDED' if added else 'ALREADY_PRESENT'}")
        print("Windows OpenSSH BatchMode verification: PASS")
        print(f"Probe files: {config.work_dir}")
    finally:
        try:
            session.close()
        except Exception:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isolated RAB SSH bootstrap feasibility probe")
    parser.add_argument("--host", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = ProbeConfig(args.host, args.username, args.port, args.work_dir.resolve(), args.timeout)
    try:
        try:
            import paramiko
        except ImportError:
            raise ProbeError(
                "PARAMIKO_NOT_INSTALLED",
                "Paramiko is required only in the isolated Spike environment",
            ) from None
        run_probe(config, paramiko)
        return 0
    except ProbeError as exc:
        print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
