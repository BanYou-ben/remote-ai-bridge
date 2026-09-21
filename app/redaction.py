from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*)([^\s]+(?:\s+[^\s]+)?)"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s&]+"),
    re.compile(r"(?i)((?:access[_-]?token|refresh[_-]?token|token|secret)\s*[=:]\s*)[^\s&]+"),
    re.compile(r"(?i)((?:password|passwd|pwd)\s*[=:]\s*)[^\s&]+"),
    re.compile(r"(?i)((?:cookie|set-cookie)\s*:\s*)[^\r\n]+"),
    re.compile(r"(?i)(https?://[^:/\s]+:)[^@\s]+@"),
    re.compile(
        r"(?is)(-----BEGIN [^-\r\n]*PRIVATE KEY-----).*?(-----END [^-\r\n]*PRIVATE KEY-----)"
    ),
)

SENSITIVE_DETAIL_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "cookie",
    "set_cookie",
    "private_key",
}

SENSITIVE_DETAIL_SUFFIXES = (
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "api_key",
    "private_key",
)


def redact(value: str) -> str:
    redacted = str(value)
    for pattern in SECRET_PATTERNS:
        if "PRIVATE KEY" in pattern.pattern:
            redacted = pattern.sub(r"\1\n[REDACTED]\n\2", redacted)
        else:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def redact_details(value: Any, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {str(item_key): redact_details(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_details(item) for item in value)
    if isinstance(value, list):
        return [redact_details(item) for item in value]
    if isinstance(value, set):
        return {redact_details(item) for item in value}
    return value


def _normalise_key(value: str) -> str:
    snake_case = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")


def _is_sensitive_key(value: str) -> bool:
    normalised = _normalise_key(value)
    if normalised in SENSITIVE_DETAIL_KEYS:
        return True
    if any(normalised.endswith(f"_{suffix}") for suffix in SENSITIVE_DETAIL_SUFFIXES):
        return True
    return (
        normalised.startswith("authorization_")
        or normalised.endswith("_authorization")
        or normalised.endswith("_cookie")
        or normalised.startswith("private_key_")
    )
