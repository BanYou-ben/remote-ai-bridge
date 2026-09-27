from __future__ import annotations

from dataclasses import dataclass
import re

from app.services.ai.schemas import DiagnosticContext, DiagnosticEvidence


VALID_CONFIDENCE_LEVELS: frozenset[str] = frozenset({"low", "medium", "high"})
DIAGNOSIS_STAGE_RE = re.compile(
    r"^(?:unknown|[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)$"
)


class DiagnosisValidationError(ValueError):
    """Raised when a structured diagnosis violates its trusted contract."""


@dataclass(frozen=True)
class StructuredDiagnosis:
    summary: str
    diagnosis_stage: str
    evidence_ids: tuple[str, ...]
    possible_causes: tuple[str, ...]
    recommended_actions: tuple[str, ...]
    confidence: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise DiagnosisValidationError("diagnosis summary must not be empty")
        if not isinstance(self.diagnosis_stage, str) or not self.diagnosis_stage.strip():
            raise DiagnosisValidationError("diagnosis stage must not be empty")
        if not DIAGNOSIS_STAGE_RE.fullmatch(self.diagnosis_stage):
            raise DiagnosisValidationError("diagnosis stage must be a lowercase namespace or unknown")
        if not isinstance(self.confidence, str) or self.confidence not in VALID_CONFIDENCE_LEVELS:
            raise DiagnosisValidationError("diagnosis confidence must be low, medium, or high")
        self._validate_strings("evidence_ids", self.evidence_ids)
        self._validate_strings("possible_causes", self.possible_causes)
        self._validate_strings("recommended_actions", self.recommended_actions)
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise DiagnosisValidationError("diagnosis evidence IDs must not contain duplicates")
        if not self.evidence_ids:
            raise DiagnosisValidationError("diagnosis must reference at least one evidence ID")

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "diagnosis_stage": self.diagnosis_stage,
            "evidence_ids": list(self.evidence_ids),
            "possible_causes": list(self.possible_causes),
            "recommended_actions": list(self.recommended_actions),
            "confidence": self.confidence,
        }

    @staticmethod
    def _validate_strings(field_name: str, values: object) -> None:
        if not isinstance(values, tuple) or any(not isinstance(value, str) for value in values):
            raise DiagnosisValidationError(f"{field_name} must be a tuple of strings")


def validate_diagnosis(
    diagnosis: StructuredDiagnosis,
    context: DiagnosticContext,
) -> None:
    diagnosis.validate()
    evidence_by_id = {evidence.id: evidence for evidence in context.evidence}
    available_ids = set(evidence_by_id)
    unknown_ids = tuple(
        evidence_id
        for evidence_id in diagnosis.evidence_ids
        if evidence_id not in available_ids
    )
    if unknown_ids:
        raise DiagnosisValidationError("diagnosis references evidence that is not present in context")
    if diagnosis.diagnosis_stage == "unknown":
        return
    stage_domain = diagnosis.diagnosis_stage.split(".", 1)[0]
    if not any(
        evidence_by_id[evidence_id].category == stage_domain
        for evidence_id in diagnosis.evidence_ids
    ):
        raise DiagnosisValidationError(
            "diagnosis stage is not grounded by the referenced evidence categories"
        )


def resolve_evidence(
    diagnosis: StructuredDiagnosis,
    context: DiagnosticContext,
) -> tuple[DiagnosticEvidence, ...]:
    validate_diagnosis(diagnosis, context)
    evidence_by_id = {evidence.id: evidence for evidence in context.evidence}
    return tuple(evidence_by_id[evidence_id] for evidence_id in diagnosis.evidence_ids)
