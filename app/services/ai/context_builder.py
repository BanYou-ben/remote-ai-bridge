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
        checks = (
            ("connection.proxy.tcp", doctor.local.tcp),
            ("connection.proxy.handshake", doctor.local.handshake),
            ("connection.proxy.endpoint", doctor.local.endpoint),
            ("connection.ssh.config", doctor.ssh),
            ("connection.tunnel.process", doctor.tunnel),
            ("connection.remote.listener", doctor.remote_listener),
            ("connection.remote.endpoint", doctor.remote_endpoint),
        )
        evidence = tuple(
            self._evidence(evidence_id, check)
            for evidence_id, check in checks
        )
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

    @staticmethod
    def _evidence(evidence_id: str, check: CheckResult) -> DiagnosticEvidence:
        return DiagnosticEvidence(
            id=evidence_id,
            category="connection",
            name=check.name,
            status=check.status.value,
            detail=check.detail,
            error_code=check.error_code,
            http_status=check.http_status,
        )
