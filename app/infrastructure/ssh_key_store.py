from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

from app.domain.errors import RABError
from app.domain.profile import Profile
from app.domain.ssh_bootstrap import SSHKeyPair
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.profile_store import default_state_root


KEY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
KEY_COMMENT_PREFIX = "remote-ai-bridge:"


def managed_ssh_options(profile: Profile, root: Path) -> list[str]:
    """Return Windows OpenSSH options for a managed profile; legacy stays unchanged."""
    profile.validate()
    if profile.schema_version == 1:
        return []
    key_id = str(profile.key_id)
    keys_dir = root / "ssh" / "keys"
    private_key = keys_dir / f"id_ed25519_{key_id}"
    ssh_dir = root / "ssh"
    return [
        "-F",
        str(ssh_dir / "empty_config"),
        "-o",
        f"UserKnownHostsFile={ssh_dir / 'known_hosts'}",
        "-o",
        f"GlobalKnownHostsFile={ssh_dir / 'empty_global_known_hosts'}",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-i",
        str(private_key),
        "-p",
        str(profile.ssh_port),
    ]


class SSHKeyStore:
    def __init__(
        self,
        root: Path | None = None,
        runner: ProcessRunner | None = None,
        ssh_keygen: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.root = root or default_state_root()
        self.keys_dir = self.root / "ssh" / "keys"
        self.runner = runner or ProcessRunner()
        self.ssh_keygen = ssh_keygen
        self.timeout = timeout

    def ensure_key(self, key_id: str | None = None) -> SSHKeyPair:
        if key_id is not None:
            return self.load(key_id)
        return self._generate()

    def load(self, key_id: str) -> SSHKeyPair:
        self._validate_key_id(key_id)
        private_path, public_path = self._paths(key_id)
        public_key = self._read_and_validate_pair(key_id, private_path, public_path)
        return SSHKeyPair(key_id, str(private_path), str(public_path), public_key, False)

    def remove_key(self, key_id: str, expected_public_key: str) -> None:
        pair = self.load(key_id)
        if pair.public_key != expected_public_key:
            raise RABError("SSH_KEY_IDENTITY_MISMATCH", "RAB SSH key identity could not be verified")
        private_path, public_path = self._paths(key_id)
        # Remove the sensitive private key first. If deleting the public file
        # then fails, only non-secret material remains for manual cleanup.
        for path in (private_path, public_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise RABError("SSH_KEY_DELETE_FAILED", "the verified RAB SSH key could not be removed") from None

    def remove_generated(self, pair: SSHKeyPair) -> None:
        if not pair.generated:
            return
        private_path, public_path = self._paths(pair.key_id)
        if str(private_path) != pair.private_key_path or str(public_path) != pair.public_key_path:
            raise RABError("SETUP_ROLLBACK_FAILED", "generated SSH key ownership could not be verified")
        try:
            current_public_key = self._read_and_validate_pair(pair.key_id, private_path, public_path)
        except RABError:
            raise RABError("SETUP_ROLLBACK_FAILED", "generated SSH key ownership could not be verified") from None
        if current_public_key != pair.public_key:
            raise RABError("SETUP_ROLLBACK_FAILED", "generated SSH key ownership could not be verified")
        for path in (public_path, private_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise RABError("SETUP_ROLLBACK_FAILED", "generated SSH key could not be removed") from None

    def _generate(self) -> SSHKeyPair:
        key_id = uuid.uuid4().hex
        private_path, public_path = self._paths(key_id)
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        executable = self.ssh_keygen or shutil.which("ssh-keygen.exe") or shutil.which("ssh-keygen")
        if executable is None:
            raise RABError("SSH_KEY_GENERATION_FAILED", "Windows ssh-keygen was not found")
        argv = [
            executable,
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            f"{KEY_COMMENT_PREFIX}{key_id}",
            "-f",
            str(private_path),
        ]
        result = self.runner.run(argv, self.timeout)
        if not result.ok:
            self._remove_paths(private_path, public_path)
            raise RABError("SSH_KEY_GENERATION_FAILED", "Windows ssh-keygen could not create the RAB key")
        try:
            public_key = self._read_and_validate_pair(key_id, private_path, public_path)
            self._restrict_private_key(private_path)
        except RABError:
            self._remove_paths(private_path, public_path)
            raise
        return SSHKeyPair(key_id, str(private_path), str(public_path), public_key, True)

    def _read_and_validate_pair(self, key_id: str, private_path: Path, public_path: Path) -> str:
        if private_path.is_symlink() or public_path.is_symlink():
            raise RABError("SSH_KEY_UNSAFE", "RAB SSH key files must not be symbolic links")
        try:
            private_mode = private_path.stat().st_mode
            public_mode = public_path.stat().st_mode
        except OSError:
            raise RABError("SSH_KEY_NOT_FOUND", "the RAB SSH key pair is incomplete") from None
        if not stat.S_ISREG(private_mode) or not stat.S_ISREG(public_mode):
            raise RABError("SSH_KEY_UNSAFE", "RAB SSH key paths must be regular files")
        try:
            value = public_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            raise RABError("SSH_KEY_INVALID", "the RAB public key could not be read") from None
        parts = value.split()
        expected_comment = f"{KEY_COMMENT_PREFIX}{key_id}"
        if len(parts) != 3 or parts[0] != "ssh-ed25519" or parts[2] != expected_comment:
            raise RABError("SSH_KEY_INVALID", "the RAB public key metadata is invalid")
        try:
            base64.b64decode(parts[1].encode("ascii"), validate=True)
        except Exception:
            raise RABError("SSH_KEY_INVALID", "the RAB public key data is invalid") from None
        return value

    @staticmethod
    def _restrict_private_key(path: Path) -> None:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            raise RABError("SSH_KEY_PERMISSION_FAILED", "RAB private key permissions could not be restricted") from None

    @staticmethod
    def _remove_paths(*paths: Path) -> None:
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass

    def _paths(self, key_id: str) -> tuple[Path, Path]:
        self._validate_key_id(key_id)
        private_path = self.keys_dir / f"id_ed25519_{key_id}"
        return private_path, private_path.with_suffix(".pub")

    @staticmethod
    def _validate_key_id(key_id: str) -> None:
        if not KEY_ID_RE.fullmatch(key_id):
            raise RABError("SSH_KEY_ID_INVALID", "RAB SSH key ID is invalid")
