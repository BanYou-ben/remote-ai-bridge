from __future__ import annotations

from dataclasses import asdict, dataclass
import ipaddress
import re
import uuid
from urllib.parse import urlsplit

from app.domain.ssh_bootstrap import is_valid_host_key_type


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SSH_TARGET_RE = re.compile(r"^(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.:-]*$")
REMOTE_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
MANAGED_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
MANAGED_KEY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
HOST_KEY_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{20,}={0,2}$")
CURRENT_PROFILE_SCHEMA_VERSION = 2
SUPPORTED_PROFILE_SCHEMA_VERSIONS = frozenset({1, 2})


class ProfileValidationError(ValueError):
    """Raised when a profile violates a Phase 1 safety constraint."""


@dataclass(frozen=True)
class Profile:
    schema_version: int
    name: str
    ssh_target: str
    local_proxy_host: str = "127.0.0.1"
    local_proxy_port: int = 7897
    remote_bind_host: str = "127.0.0.1"
    remote_port: int = 17890
    auto_reconnect: bool = True
    endpoint_probe_url: str = "https://api.openai.com/v1/models"
    profile_type: str = "legacy"
    host: str | None = None
    username: str | None = None
    ssh_port: int | None = None
    key_id: str | None = None
    host_key_type: str | None = None
    host_key_fingerprint: str | None = None

    def validate(self) -> None:
        if self.schema_version not in SUPPORTED_PROFILE_SCHEMA_VERSIONS:
            raise ProfileValidationError(f"unsupported profile schema_version: {self.schema_version!r}")
        if not PROFILE_NAME_RE.fullmatch(self.name):
            raise ProfileValidationError("invalid profile name")
        if not SSH_TARGET_RE.fullmatch(self.ssh_target) or self.ssh_target.startswith("-"):
            raise ProfileValidationError("invalid or unsafe SSH target")
        if self.local_proxy_host != "127.0.0.1":
            raise ProfileValidationError("local proxy must bind to 127.0.0.1")
        if self.remote_bind_host != "127.0.0.1":
            raise ProfileValidationError("remote forwarding must bind to 127.0.0.1")
        for label, port in (
            ("local_proxy_port", self.local_proxy_port),
            ("remote_port", self.remote_port),
        ):
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ProfileValidationError(f"{label} must be an integer from 1 to 65535")
        parsed = urlsplit(self.endpoint_probe_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ProfileValidationError("endpoint probe URL must be an HTTPS URL without credentials, query, or fragment")
        managed_values = (
            self.host,
            self.username,
            self.ssh_port,
            self.key_id,
            self.host_key_type,
            self.host_key_fingerprint,
        )
        if self.schema_version == 1:
            if self.profile_type != "legacy" or any(value is not None for value in managed_values):
                raise ProfileValidationError("legacy v1 profiles cannot contain managed SSH metadata")
            return
        if self.profile_type != "managed":
            raise ProfileValidationError("schema v2 profiles must use managed profile_type")
        if any(value is None for value in managed_values):
            raise ProfileValidationError("managed profile SSH metadata is incomplete")
        if not MANAGED_HOST_RE.fullmatch(str(self.host)):
            raise ProfileValidationError("invalid managed SSH host")
        if not REMOTE_USER_RE.fullmatch(str(self.username)):
            raise ProfileValidationError("invalid managed SSH username")
        if isinstance(self.ssh_port, bool) or not isinstance(self.ssh_port, int) or not 1 <= self.ssh_port <= 65535:
            raise ProfileValidationError("managed SSH port must be an integer from 1 to 65535")
        if not MANAGED_KEY_ID_RE.fullmatch(str(self.key_id)):
            raise ProfileValidationError("invalid managed SSH key ID")
        if not is_valid_host_key_type(self.host_key_type):
            raise ProfileValidationError("invalid managed SSH host key type")
        if not HOST_KEY_FINGERPRINT_RE.fullmatch(str(self.host_key_fingerprint)):
            raise ProfileValidationError("invalid managed SSH host key fingerprint")
        if self.ssh_target != f"{self.username}@{self.host}":
            raise ProfileValidationError("managed ssh_target must match username and host")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        values = asdict(self)
        if self.schema_version == 1:
            for key in (
                "profile_type",
                "host",
                "username",
                "ssh_port",
                "key_id",
                "host_key_type",
                "host_key_fingerprint",
            ):
                values.pop(key)
        return values

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Profile":
        schema_version = data.get("schema_version")
        if schema_version not in SUPPORTED_PROFILE_SCHEMA_VERSIONS:
            raise ProfileValidationError(f"unsupported profile schema_version: {schema_version!r}")
        try:
            values = dict(data)
            if schema_version == 1:
                values.setdefault("profile_type", "legacy")
            profile = cls(**values)
        except TypeError as exc:
            raise ProfileValidationError(f"invalid profile fields: {exc}") from exc
        profile.validate()
        return profile


@dataclass(frozen=True)
class RemoteTunnelIdentity:
    tunnel_id: str
    remote_port: int
    remote_user: str
    sshd_pid: int
    sshd_start_ticks: int
    boot_id: str
    ssh_connection: str

    def validate(self) -> None:
        try:
            tunnel_uuid = uuid.UUID(self.tunnel_id)
        except (ValueError, AttributeError) as exc:
            raise ProfileValidationError("remote tunnel ID must be a UUID") from exc
        if str(tunnel_uuid) != self.tunnel_id.lower():
            raise ProfileValidationError("remote tunnel ID must use canonical UUID form")
        if not 1 <= self.remote_port <= 65535:
            raise ProfileValidationError("invalid remote identity port")
        if not REMOTE_USER_RE.fullmatch(self.remote_user):
            raise ProfileValidationError("invalid remote user")
        if self.sshd_pid <= 0:
            raise ProfileValidationError("invalid remote process identity")
        if self.sshd_start_ticks <= 0:
            raise ProfileValidationError("invalid remote sshd start time")
        try:
            boot_uuid = uuid.UUID(self.boot_id)
        except (ValueError, AttributeError) as exc:
            raise ProfileValidationError("invalid remote boot ID") from exc
        if str(boot_uuid) != self.boot_id.lower():
            raise ProfileValidationError("remote boot ID must use canonical UUID form")
        connection = self.ssh_connection.split()
        if len(connection) != 4:
            raise ProfileValidationError("invalid SSH_CONNECTION identity")
        try:
            ipaddress.ip_address(connection[0])
            source_port = int(connection[1])
            ipaddress.ip_address(connection[2])
            destination_port = int(connection[3])
        except ValueError as exc:
            raise ProfileValidationError("invalid SSH_CONNECTION identity") from exc
        if not 1 <= source_port <= 65535 or not 1 <= destination_port <= 65535:
            raise ProfileValidationError("invalid SSH_CONNECTION ports")


@dataclass(frozen=True)
class RuntimeState:
    schema_version: int
    profile_name: str
    pid: int
    process_creation_time: float
    executable_path: str
    tunnel_id: str
    supervisor_pid: int
    remote_port: int
    started_at: str
    last_successful_probe_at: str | None = None
    remote_user: str | None = None
    remote_sshd_pid: int | None = None
    remote_sshd_start_ticks: int | None = None
    remote_boot_id: str | None = None
    remote_ssh_connection: str | None = None

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ProfileValidationError("runtime schema_version must be 1")
        if not PROFILE_NAME_RE.fullmatch(self.profile_name):
            raise ProfileValidationError("invalid runtime profile name")
        if self.pid <= 0 or self.supervisor_pid <= 0:
            raise ProfileValidationError("runtime PIDs must be positive")
        if self.process_creation_time <= 0:
            raise ProfileValidationError("invalid process creation time")
        if not self.tunnel_id:
            raise ProfileValidationError("missing tunnel ID")
        if not 1 <= self.remote_port <= 65535:
            raise ProfileValidationError("invalid runtime remote port")
        remote_values = (
            self.remote_user,
            self.remote_sshd_pid,
            self.remote_sshd_start_ticks,
            self.remote_boot_id,
            self.remote_ssh_connection,
        )
        if any(value is not None for value in remote_values):
            if any(value is None for value in remote_values):
                raise ProfileValidationError("incomplete remote tunnel identity")
            self.remote_identity().validate()

    @property
    def has_remote_identity(self) -> bool:
        return self.remote_user is not None

    def remote_identity(self) -> RemoteTunnelIdentity:
        if not self.has_remote_identity:
            raise ProfileValidationError("runtime has no verified remote tunnel identity")
        return RemoteTunnelIdentity(
            tunnel_id=self.tunnel_id,
            remote_port=self.remote_port,
            remote_user=str(self.remote_user),
            sshd_pid=int(self.remote_sshd_pid),
            sshd_start_ticks=int(self.remote_sshd_start_ticks),
            boot_id=str(self.remote_boot_id),
            ssh_connection=str(self.remote_ssh_connection),
        )

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "RuntimeState":
        # Accept runtime files written by the short-lived listener-PID implementation.
        # The field was never reliable without privileged process visibility and is ignored.
        normalized = dict(data)
        normalized.pop("remote_listener_pid", None)
        try:
            state = cls(**normalized)
        except TypeError as exc:
            raise ProfileValidationError(f"invalid runtime fields: {exc}") from exc
        state.validate()
        return state
