from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.redaction import redact, redact_details


class RABError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = redact(message)
        self.retryable = bool(retryable)
        self.details = redact_details(dict(details or {}))
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
