from __future__ import annotations

import base64
import os
from pathlib import Path
import tempfile

from app.domain.errors import RABError
from app.domain.ssh_bootstrap import HostKeyInfo, is_valid_host_key_type
from app.infrastructure.profile_store import default_state_root


class HostKeyStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_state_root()
        self.path = self.root / "ssh" / "known_hosts"

    def status(self, observed: HostKeyInfo) -> str:
        self._validate_key(observed)
        matches = self._entries_for(observed.host_token)
        if not matches:
            return "unknown"
        expected = (observed.key_type, observed.key_base64)
        if any(entry == expected for entry in matches):
            return "confirmed"
        raise RABError(
            "HOST_KEY_CHANGED",
            "the SSH host key differs from the previously confirmed key",
            details={
                "host": observed.host,
                "port": observed.port,
                "key_type": observed.key_type,
                "fingerprint": observed.fingerprint,
            },
        )

    def confirm(self, observed: HostKeyInfo, *, accepted: bool) -> bool:
        if not accepted:
            raise RABError(
                "HOST_KEY_REJECTED",
                "the SSH host key was rejected by the user",
                details={"host": observed.host, "port": observed.port},
            )
        status = self.status(observed)
        if status == "confirmed":
            return False
        self._validate_key(observed)
        lines = self._read_lines()
        lines.append(f"{observed.host_token} {observed.key_type} {observed.key_base64}")
        self._write_lines(lines)
        return True

    def remove_if_matches(self, observed: HostKeyInfo) -> bool:
        exact = f"{observed.host_token} {observed.key_type} {observed.key_base64}"
        lines = self._read_lines()
        if exact not in lines:
            return False
        updated = list(lines)
        updated.remove(exact)
        self._write_lines(updated)
        return True

    def _entries_for(self, host_token: str) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for line in self._read_lines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] == host_token:
                entries.append((parts[1], parts[2]))
        return entries

    def _read_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            value = self.path.read_text(encoding="ascii")
        except (OSError, UnicodeError):
            raise RABError("KNOWN_HOSTS_UNAVAILABLE", "RAB known_hosts could not be read") from None
        return [line for line in value.splitlines() if line.strip()]

    def _write_lines(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".known_hosts.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
                if lines:
                    handle.write("\n".join(lines) + "\n")
            os.replace(temporary_name, self.path)
        except OSError:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise RABError("KNOWN_HOSTS_WRITE_FAILED", "RAB known_hosts could not be updated") from None

    @staticmethod
    def _validate_key(observed: HostKeyInfo) -> None:
        if not is_valid_host_key_type(observed.key_type):
            raise RABError("HOST_KEY_INVALID", "the SSH server returned an invalid host key type")
        try:
            base64.b64decode(observed.key_base64.encode("ascii"), validate=True)
        except Exception:
            raise RABError("HOST_KEY_INVALID", "the SSH server returned invalid host key data") from None
