from __future__ import annotations

from app.services.ai.diagnosis import StructuredDiagnosis, validate_diagnosis
from app.services.ai.diagnosis_provider import DiagnosisProvider
from app.services.ai.schemas import DiagnosticContext


class AIDiagnosticService:
    def __init__(self, provider: DiagnosisProvider) -> None:
        self._provider = provider

    def diagnose(self, context: DiagnosticContext) -> StructuredDiagnosis:
        diagnosis = self._provider.diagnose(context)
        validate_diagnosis(diagnosis, context)
        return diagnosis
