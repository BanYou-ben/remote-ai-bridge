from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from urllib.parse import urlsplit

from app.domain.profile import ProfileValidationError, validate_profile_name


CURRENT_APPLICATION_TARGET_SCHEMA_VERSION = 1
APPLICATION_TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SYSTEMD_SERVICE_UNIT_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,247}\.service$")
APPLICATION_TARGET_FIELDS = frozenset(
    {"schema_version", "name", "profile_name", "service_unit", "health_url"}
)


class ApplicationTargetValidationError(ValueError):
    """Raised when a trusted application target violates its safety contract."""


def validate_application_target_name(name: object) -> None:
    if (
        not isinstance(name, str)
        or not APPLICATION_TARGET_NAME_RE.fullmatch(name)
        or ".." in name
    ):
        raise ApplicationTargetValidationError("invalid application target name")


def validate_service_unit(service_unit: object) -> None:
    if not isinstance(service_unit, str) or not SYSTEMD_SERVICE_UNIT_RE.fullmatch(service_unit):
        raise ApplicationTargetValidationError("invalid application target service unit")


def validate_health_url(health_url: object) -> None:
    if not isinstance(health_url, str) or not health_url:
        raise ApplicationTargetValidationError("invalid application target health URL")
    if any(ord(character) < 32 or ord(character) == 127 for character in health_url):
        raise ApplicationTargetValidationError("application target health URL contains control characters")
    try:
        parsed = urlsplit(health_url)
        port = parsed.port
    except ValueError as exc:
        raise ApplicationTargetValidationError("invalid application target health URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.netloc != f"127.0.0.1:{port}"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.path
    ):
        raise ApplicationTargetValidationError(
            "application target health URL must use an explicit remote loopback port without credentials, query, or fragment"
        )


@dataclass(frozen=True)
class ApplicationTarget:
    schema_version: int
    name: str
    profile_name: str
    service_unit: str | None = None
    health_url: str | None = None

    def validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CURRENT_APPLICATION_TARGET_SCHEMA_VERSION
        ):
            raise ApplicationTargetValidationError(
                f"unsupported application target schema_version: {self.schema_version!r}"
            )
        validate_application_target_name(self.name)
        try:
            validate_profile_name(self.profile_name)
        except ProfileValidationError as exc:
            raise ApplicationTargetValidationError("invalid application target profile name") from exc
        if self.service_unit is None and self.health_url is None:
            raise ApplicationTargetValidationError(
                "application target requires a service unit or health URL"
            )
        if self.service_unit is not None:
            validate_service_unit(self.service_unit)
        if self.health_url is not None:
            validate_health_url(self.health_url)

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ApplicationTarget":
        if not isinstance(data, dict) or set(data) != APPLICATION_TARGET_FIELDS:
            raise ApplicationTargetValidationError(
                "application target fields are missing or unknown"
            )
        try:
            target = cls(**data)
        except TypeError as exc:
            raise ApplicationTargetValidationError("invalid application target fields") from exc
        target.validate()
        return target
