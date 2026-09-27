from __future__ import annotations

from app.domain.health import CheckResult
from app.domain.profile import Profile
from app.domain.supervisor import SupervisorSnapshot
from app.services.ai.schemas import (
    DiagnosticContext,
    DiagnosticEvidence,
    DiagnosticProfileSummary,
    DiagnosticRuntimeSummary,
)
from app.services.doctor import DoctorReport
from app.services.local_proxy import LocalProxyReport
from app.redaction import redact


class DiagnosticContextBuilder:
    """Build a minimal, deterministic diagnostic snapshot without side effects."""

    def build(
        self,
        profile: Profile,
        runtime: SupervisorSnapshot,
        doctor: DoctorReport,
    ) -> DiagnosticContext:
        profile.validate()
        evidence = build_doctor_evidence(doctor)
        return DiagnosticContext(
            profile=DiagnosticProfileSummary(
                name=profile.name,
                profile_type=profile.profile_type,
                local_proxy_host=profile.local_proxy_host,
                local_proxy_port=profile.local_proxy_port,
                remote_bind_host=profile.remote_bind_host,
                remote_port=profile.remote_port,
                auto_reconnect=profile.auto_reconnect,
                endpoint_probe_url=redact(profile.endpoint_probe_url),
            ),
            runtime=DiagnosticRuntimeSummary(
                state=runtime.state.value,
                attempt=runtime.attempt,
                error_code=runtime.error_code,
                message=runtime.message,
                retry_in_seconds=runtime.retry_in_seconds,
                remote_port=runtime.remote_port,
                last_successful_probe_at=runtime.last_successful_probe_at,
                suggested_port=runtime.suggested_port,
                supervised=runtime.supervised,
                runtime_present=runtime.runtime_present,
                process_alive=runtime.process_alive,
            ),
            evidence=evidence,
        )

def build_doctor_evidence(doctor: DoctorReport) -> tuple[DiagnosticEvidence, ...]:
    """Convert a Doctor report using the canonical, explicit evidence mapping."""
    return (
        *build_local_proxy_evidence(doctor.local),
        build_diagnostic_evidence("connection.ssh.config", "connection", doctor.ssh),
        build_diagnostic_evidence("connection.tunnel.process", "connection", doctor.tunnel),
        build_diagnostic_evidence(
            "connection.remote.listener", "connection", doctor.remote_listener
        ),
        build_diagnostic_evidence(
            "connection.remote.endpoint", "connection", doctor.remote_endpoint
        ),
    )


def build_local_proxy_evidence(report: LocalProxyReport) -> tuple[DiagnosticEvidence, ...]:
    return (
        build_diagnostic_evidence("connection.proxy.tcp", "connection", report.tcp),
        build_diagnostic_evidence(
            "connection.proxy.handshake", "connection", report.handshake
        ),
        build_diagnostic_evidence(
            "connection.proxy.endpoint", "connection", report.endpoint
        ),
    )


def build_diagnostic_evidence(
    evidence_id: str,
    category: str,
    check: CheckResult,
) -> DiagnosticEvidence:
    return DiagnosticEvidence(
        id=evidence_id,
        category=category,
        name=check.name,
        status=check.status.value,
        detail=check.detail,
        error_code=check.error_code,
        http_status=check.http_status,
    )
