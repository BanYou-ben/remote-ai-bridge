from __future__ import annotations

from typing import Protocol

from app.services.ai.diagnosis import StructuredDiagnosis
from app.services.ai.schemas import DiagnosticContext


class AIProviderError(RuntimeError):
    """Safe provider boundary that never exposes raw SDK exceptions."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


class DiagnosisProvider(Protocol):
    def diagnose(self, context: DiagnosticContext) -> StructuredDiagnosis:
        """Return one structured diagnosis for an already-sanitized context."""
        ...
