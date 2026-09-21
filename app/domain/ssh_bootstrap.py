from __future__ import annotations

from dataclasses import dataclass
import re


HOST_KEY_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+-]{0,127}$")


def is_valid_host_key_type(value: object) -> bool:
    return isinstance(value, str) and HOST_KEY_TYPE_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class HostKeyInfo:
    host: str
    port: int
    key_type: str
    fingerprint: str
    key_base64: str

    @property
    def host_token(self) -> str:
        return self.host if self.port == 22 else f"[{self.host}]:{self.port}"


@dataclass(frozen=True)
class HostPreparation:
    host: str
    port: int
    key_type: str
    fingerprint: str
    status: str


@dataclass(frozen=True)
class SSHKeyPair:
    key_id: str
    private_key_path: str
    public_key_path: str
    public_key: str
    generated: bool


@dataclass(frozen=True)
class AuthorizedKeyInstall:
    public_key_added: bool
    ssh_dir_created: bool = False
    authorized_keys_created: bool = False


@dataclass(frozen=True)
class BootstrapResult:
    host: str
    port: int
    username: str
    key_id: str
    host_key_type: str
    host_key_fingerprint: str
    public_key_added: bool
    batch_login_verified: bool = True
