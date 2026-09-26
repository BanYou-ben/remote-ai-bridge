from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.ai.diagnosis import (
    DiagnosisValidationError,
    StructuredDiagnosis,
    resolve_evidence,
    validate_diagnosis,
)
from app.services.ai.schemas import (
    DiagnosticContext,
    DiagnosticEvidence,
    DiagnosticProfileSummary,
    DiagnosticRuntimeSummary,
)


def diagnostic_context() -> DiagnosticContext:
    return DiagnosticContext(
        profile=DiagnosticProfileSummary(
            name="server-a",
            profile_type="managed",
            local_proxy_host="127.0.0.1",
            local_proxy_port=7897,
            remote_bind_host="127.0.0.1",
            remote_port=17890,
            auto_reconnect=True,
            endpoint_probe_url="https://api.openai.com/v1/models",
        ),
        runtime=DiagnosticRuntimeSummary(
            state="FAILED",
            attempt=1,
            error_code="PROCESS_EXITED",
            message="tunnel process exited",
            retry_in_seconds=None,
            remote_port=17890,
            last_successful_probe_at=None,
            suggested_port=None,
            supervised=True,
            runtime_present=True,
            process_alive=False,
        ),
        evidence=(
            DiagnosticEvidence(
                id="connection.ssh.config",
                category="connection",
                name="SSH configuration",
                status="PASS",
                detail="target resolves",
                error_code=None,
                http_status=None,
            ),
            DiagnosticEvidence(
                id="connection.tunnel.process",
                category="connection",
                name="Tunnel process",
                status="FAIL",
                detail="process exited",
                error_code="PROCESS_EXITED",
                http_status=None,
            ),
        ),
    )


def structured_diagnosis(
    *,
    evidence_ids: tuple[str, ...] = (
        "connection.ssh.config",
        "connection.tunnel.process",
    ),
    confidence: str = "high",
) -> StructuredDiagnosis:
    return StructuredDiagnosis(
        summary="The tunnel process exited before the remote listener became available.",
        diagnosis_stage="connection.tunnel",
        evidence_ids=evidence_ids,
        possible_causes=("The tunnel process exited during startup.",),
        recommended_actions=("Retry the connection and run diagnostics again.",),
        confidence=confidence,
    )


def test_structured_diagnosis_constructs_with_expected_fields() -> None:
    diagnosis = structured_diagnosis()

    assert diagnosis.diagnosis_stage == "connection.tunnel"
    assert diagnosis.confidence == "high"
    assert diagnosis.evidence_ids == (
        "connection.ssh.config",
        "connection.tunnel.process",
    )


def test_to_dict_is_json_serializable() -> None:
    payload = structured_diagnosis().to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert isinstance(payload["evidence_ids"], list)


@pytest.mark.parametrize("confidence", ("low", "medium", "high"))
def test_supported_confidence_levels_are_valid(confidence: str) -> None:
    assert structured_diagnosis(confidence=confidence).confidence == confidence


def test_invalid_confidence_is_rejected() -> None:
    with pytest.raises(DiagnosisValidationError, match="confidence"):
        structured_diagnosis(confidence="certain")


def test_existing_evidence_ids_pass_context_validation() -> None:
    validate_diagnosis(structured_diagnosis(), diagnostic_context())


def test_unknown_evidence_id_is_rejected() -> None:
    diagnosis = structured_diagnosis(evidence_ids=("network.magic",))

    with pytest.raises(DiagnosisValidationError, match="not present"):
        validate_diagnosis(diagnosis, diagnostic_context())


def test_duplicate_evidence_ids_are_rejected() -> None:
    with pytest.raises(DiagnosisValidationError, match="duplicates"):
        structured_diagnosis(
            evidence_ids=(
                "connection.tunnel.process",
                "connection.tunnel.process",
            )
        )


def test_resolve_evidence_returns_real_context_evidence() -> None:
    context = diagnostic_context()

    resolved = resolve_evidence(structured_diagnosis(), context)

    assert resolved == context.evidence
    assert resolved[1].detail == "process exited"


def test_resolve_evidence_preserves_diagnosis_reference_order() -> None:
    diagnosis = structured_diagnosis(
        evidence_ids=(
            "connection.tunnel.process",
            "connection.ssh.config",
        )
    )

    resolved = resolve_evidence(diagnosis, diagnostic_context())

    assert tuple(evidence.id for evidence in resolved) == diagnosis.evidence_ids


def test_empty_summary_is_rejected() -> None:
    with pytest.raises(DiagnosisValidationError, match="summary"):
        StructuredDiagnosis(
            summary="  ",
            diagnosis_stage="connection.tunnel",
            evidence_ids=(),
            possible_causes=(),
            recommended_actions=(),
            confidence="low",
        )


def test_empty_diagnosis_stage_is_rejected() -> None:
    with pytest.raises(DiagnosisValidationError, match="stage"):
        StructuredDiagnosis(
            summary="No stage was provided.",
            diagnosis_stage="",
            evidence_ids=(),
            possible_causes=(),
            recommended_actions=(),
            confidence="low",
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    (
        ("evidence_ids", ["connection.ssh.config"]),
        ("possible_causes", (1,)),
        ("recommended_actions", (None,)),
    ),
)
def test_string_collections_require_tuples_of_strings(
    field_name: str,
    invalid_value: object,
) -> None:
    values: dict[str, object] = {
        "summary": "A structured diagnosis.",
        "diagnosis_stage": "connection.ssh",
        "evidence_ids": (),
        "possible_causes": (),
        "recommended_actions": (),
        "confidence": "low",
    }
    values[field_name] = invalid_value

    with pytest.raises(DiagnosisValidationError, match=field_name):
        StructuredDiagnosis(**values)


def test_serialization_is_deterministic() -> None:
    diagnosis = structured_diagnosis()

    assert diagnosis.to_dict() == diagnosis.to_dict()


def test_possible_causes_never_become_context_evidence() -> None:
    context = diagnostic_context()
    diagnosis = structured_diagnosis()
    before = context.to_dict()

    validate_diagnosis(diagnosis, context)

    assert context.to_dict() == before
    assert diagnosis.possible_causes[0] not in json.dumps(context.to_dict())


def test_recommended_actions_do_not_trigger_business_operations() -> None:
    context = diagnostic_context()
    diagnosis = structured_diagnosis()
    before = context.to_dict()

    resolved = resolve_evidence(diagnosis, context)

    assert context.to_dict() == before
    assert resolved == context.evidence
    assert diagnosis.recommended_actions == (
        "Retry the connection and run diagnostics again.",
    )


def test_diagnosis_contract_has_no_model_or_application_framework_dependency() -> None:
    source = Path("app/services/ai/diagnosis.py").read_text(encoding="utf-8").lower()

    for forbidden in (
        "openai",
        "anthropic",
        "gemini",
        "langchain",
        "langgraph",
        "fastapi",
        "app.services.runtime_manager",
        "app.services.tunnel",
    ):
        assert forbidden not in source
