from __future__ import annotations

from dataclasses import dataclass
import threading

from app.domain.health import CheckResult
from app.domain.profile import Profile
from app.domain.supervisor import SupervisorSnapshot, SupervisorState, utc_now
from app.infrastructure.profile_store import ProfileLockError, ProfileNotFoundError, ProfileStore
from app.redaction import redact
from app.services.local_proxy import LocalProxyReport, LocalProxyService
from app.services.tunnel import ActiveTunnel, RemotePortConflictError, TunnelError, TunnelManager


@dataclass(frozen=True)
class SupervisorPolicy:
    backoff_seconds: tuple[float, ...] = (1, 2, 5, 10, 30)
    health_interval_seconds: float = 15.0
    verify_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not self.backoff_seconds or any(value < 0 for value in self.backoff_seconds):
            raise ValueError("backoff_seconds must contain non-negative values")
        if self.health_interval_seconds < 0 or self.verify_timeout_seconds < 0:
            raise ValueError("supervisor intervals must be non-negative")


class TunnelSupervisor:
    """Owns the long-running state machine for exactly one profile."""

    def __init__(
        self,
        profile_name: str,
        store: ProfileStore,
        local_proxy: LocalProxyService,
        tunnel_manager: TunnelManager,
        policy: SupervisorPolicy | None = None,
    ) -> None:
        self.profile_name = profile_name
        self.store = store
        self.local_proxy = local_proxy
        self.tunnel_manager = tunnel_manager
        self.policy = policy or SupervisorPolicy()
        self._snapshot_lock = threading.RLock()
        self._snapshot = SupervisorSnapshot(
            profile_name=profile_name,
            state=SupervisorState.STARTING,
            updated_at=utc_now(),
            message="supervisor is starting",
        )

    def snapshot(self) -> SupervisorSnapshot:
        with self._snapshot_lock:
            return self._snapshot

    def run(self, stop_event: threading.Event) -> SupervisorSnapshot:
        self._publish(SupervisorState.STARTING, message="supervisor is starting")
        try:
            with self.store.supervisor_lock(self.profile_name):
                try:
                    profile = self.store.load_profile(self.profile_name)
                except ProfileNotFoundError as exc:
                    return self._publish(
                        SupervisorState.FAILED,
                        error_code="PROFILE_NOT_FOUND",
                        message=str(exc),
                    )
                return self._run_locked(profile, stop_event)
        except ProfileLockError as exc:
            return self._publish(
                SupervisorState.FAILED,
                error_code="PROFILE_BUSY",
                message=str(exc),
            )
        except Exception as exc:
            return self.fail_unexpected(exc)

    def fail_unexpected(self, exc: BaseException) -> SupervisorSnapshot:
        return self._publish(
            SupervisorState.FAILED,
            error_code="SUPERVISOR_FAILED",
            message=f"unexpected supervisor failure: {redact(str(exc))}",
        )

    def _run_locked(self, profile: Profile, stop_event: threading.Event) -> SupervisorSnapshot:
        active: ActiveTunnel | None = None
        attempt = 0
        backoff_index = 0
        try:
            while not stop_event.is_set():
                if active is not None and active.poll() is not None:
                    active = None
                    attempt += 1
                    delay = self._backoff(backoff_index)
                    backoff_index += 1
                    self._publish(
                        SupervisorState.DEGRADED,
                        attempt=attempt,
                        error_code="TUNNEL_EXITED",
                        message=(
                            "owned SSH process exited; runtime ownership evidence is retained "
                            "for safe recovery"
                        ),
                        retry_in_seconds=delay,
                    )
                    if not profile.auto_reconnect:
                        return self._publish(
                            SupervisorState.FAILED,
                            attempt=attempt,
                            error_code="TUNNEL_EXITED",
                            message="owned SSH process exited and automatic reconnect is disabled",
                        )
                    if stop_event.wait(delay):
                        break

                local_report = self.local_proxy.inspect(profile)
                local_failure = self._local_failure(local_report)
                if local_failure is not None:
                    check, message = local_failure
                    if not profile.auto_reconnect:
                        if active is not None:
                            cleanup = self._cleanup_active(profile, active)
                            if not cleanup[0]:
                                return self._cleanup_failure(cleanup, check.error_code, message, active)
                            active = None
                        return self._publish(
                            SupervisorState.FAILED,
                            attempt=attempt,
                            error_code=check.error_code or "LOCAL_PROXY_UNHEALTHY",
                            message=message,
                        )
                    attempt += 1
                    delay = self._backoff(backoff_index)
                    backoff_index += 1
                    self._publish(
                        SupervisorState.DEGRADED,
                        attempt=attempt,
                        error_code=check.error_code or "LOCAL_PROXY_UNHEALTHY",
                        message=message,
                        retry_in_seconds=delay,
                        active=active,
                    )
                    if stop_event.wait(delay):
                        break
                    continue

                if stop_event.is_set():
                    break

                if active is None:
                    self._publish(
                        SupervisorState.CONNECTING,
                        attempt=attempt,
                        message="acquiring SSH reverse tunnel",
                    )
                    try:
                        active = self.tunnel_manager.acquire(profile)
                    except RemotePortConflictError as exc:
                        return self._publish(
                            SupervisorState.FAILED,
                            attempt=attempt,
                            error_code=exc.error_code,
                            message=str(exc),
                            suggested_port=exc.suggested_port,
                        )
                    except TunnelError as exc:
                        if not profile.auto_reconnect:
                            return self._publish(
                                SupervisorState.FAILED,
                                attempt=attempt,
                                error_code=exc.error_code,
                                message=str(exc),
                            )
                        attempt += 1
                        delay = self._backoff(backoff_index)
                        backoff_index += 1
                        self._publish(
                            SupervisorState.DEGRADED,
                            attempt=attempt,
                            error_code=exc.error_code,
                            message=str(exc),
                            retry_in_seconds=delay,
                        )
                        if stop_event.wait(delay):
                            break
                        continue

                if stop_event.is_set():
                    break

                listener, endpoint = self.tunnel_manager.verify(
                    profile,
                    active,
                    timeout=self.policy.verify_timeout_seconds,
                )
                failed_check = listener if not listener.passed else endpoint if not endpoint.passed else None
                if failed_check is not None:
                    if active.poll() is not None:
                        active = None
                        message = "owned SSH process exited; runtime ownership evidence is retained"
                    else:
                        message = "tunnel health check failed; keeping the live owned SSH process for recovery"
                    if not profile.auto_reconnect:
                        cleanup = self._cleanup_active(profile, active)
                        if not cleanup[0]:
                            return self._cleanup_failure(
                                cleanup,
                                failed_check.error_code,
                                message,
                                active,
                            )
                        active = None
                        return self._publish(
                            SupervisorState.FAILED,
                            attempt=attempt,
                            error_code=failed_check.error_code or "TUNNEL_UNHEALTHY",
                            message=message,
                            active=active,
                        )
                    attempt += 1
                    delay = self._backoff(backoff_index)
                    backoff_index += 1
                    self._publish(
                        SupervisorState.DEGRADED,
                        attempt=attempt,
                        error_code=failed_check.error_code or "TUNNEL_UNHEALTHY",
                        message=message,
                        retry_in_seconds=delay,
                        active=active,
                    )
                    if stop_event.wait(delay):
                        break
                    continue

                attempt = 0
                backoff_index = 0
                self._publish(
                    SupervisorState.READY,
                    message="bridge is healthy",
                    active=active,
                    process_alive=True,
                )
                if stop_event.wait(self.policy.health_interval_seconds):
                    break

            return self._stop(profile, active)
        except Exception as exc:
            cleanup = (
                self._cleanup_active(profile, active)
                if active is not None
                else (True, None, "no active tunnel to clean up")
            )
            message = f"unexpected supervisor failure: {redact(str(exc))}"
            if not cleanup[0]:
                return self._publish(
                    SupervisorState.FAILED,
                    error_code=cleanup[1] or "PROCESS_STOP_FAILED",
                    message=f"{message}; cleanup failed: {cleanup[2]}",
                    active=active,
                )
            return self._publish(
                SupervisorState.FAILED,
                error_code="SUPERVISOR_FAILED",
                message=message,
                active=None,
            )

    def _stop(self, profile: Profile, active: ActiveTunnel | None) -> SupervisorSnapshot:
        self._publish(SupervisorState.STOPPING, message="stopping owned tunnel", active=active)
        stopped, error_code, message = self._cleanup_active(profile, active)
        if not stopped:
            return self._publish(
                SupervisorState.FAILED,
                error_code=error_code,
                message=message,
                active=active,
            )
        return self._publish(SupervisorState.STOPPED, message="supervision stopped")

    def _cleanup_failure(
        self,
        cleanup: tuple[bool, str | None, str],
        trigger_code: str | None,
        trigger_message: str,
        active: ActiveTunnel | None,
    ) -> SupervisorSnapshot:
        _, cleanup_code, cleanup_message = cleanup
        trigger = trigger_code or "UNKNOWN_TRIGGER"
        return self._publish(
            SupervisorState.FAILED,
            error_code=cleanup_code or "PROCESS_STOP_FAILED",
            message=f"{cleanup_message}; original trigger {trigger}: {trigger_message}",
            active=active,
        )

    def _cleanup_active(
        self,
        profile: Profile,
        active: ActiveTunnel | None,
    ) -> tuple[bool, str | None, str]:
        if active is not None:
            if not self.tunnel_manager.inspector.matches(active.state):
                return (
                    False,
                    "PROCESS_IDENTITY_MISMATCH",
                    "process identity could not be verified; runtime ownership evidence was retained",
                )
            if not active.stop(self.tunnel_manager.stop_timeout):
                return (
                    False,
                    "PROCESS_STOP_TIMEOUT",
                    "owned SSH process did not stop; runtime ownership evidence was retained",
                )
            self.store.clear_runtime(profile.name)
            return True, None, "owned SSH process stopped"

        state = self.store.load_runtime(profile.name)
        if state is None:
            return True, None, "already stopped"
        if not self.tunnel_manager.inspector.matches(state):
            return (
                False,
                "PROCESS_IDENTITY_MISMATCH",
                "process identity could not be verified; runtime ownership evidence was retained",
            )
        result = self.tunnel_manager.disconnect(profile.name)
        if not result.passed:
            return False, result.error_code or "PROCESS_STOP_FAILED", result.detail
        return True, None, result.detail

    def _publish(
        self,
        state: SupervisorState,
        *,
        attempt: int = 0,
        error_code: str | None = None,
        message: str = "",
        retry_in_seconds: float | None = None,
        suggested_port: int | None = None,
        active: ActiveTunnel | None = None,
        process_alive: bool | None = None,
    ) -> SupervisorSnapshot:
        runtime = active.state if active is not None else self.store.load_runtime(self.profile_name)
        snapshot = SupervisorSnapshot(
            profile_name=self.profile_name,
            state=state,
            updated_at=utc_now(),
            attempt=attempt,
            error_code=error_code,
            message=message,
            retry_in_seconds=retry_in_seconds,
            pid=runtime.pid if runtime is not None else None,
            tunnel_id=runtime.tunnel_id if runtime is not None else None,
            remote_port=runtime.remote_port if runtime is not None else None,
            last_successful_probe_at=runtime.last_successful_probe_at if runtime is not None else None,
            suggested_port=suggested_port,
            supervised=True,
            runtime_present=runtime is not None,
            process_alive=process_alive,
        )
        with self._snapshot_lock:
            self._snapshot = snapshot
        return snapshot

    def _backoff(self, index: int) -> float:
        return self.policy.backoff_seconds[min(index, len(self.policy.backoff_seconds) - 1)]

    @staticmethod
    def _local_failure(report: LocalProxyReport) -> tuple[CheckResult, str] | None:
        if not report.tcp.passed:
            return report.tcp, "local proxy unhealthy: TCP check failed"
        if not report.handshake.passed:
            return report.handshake, "local proxy unhealthy: HTTP proxy handshake failed"
        if not report.endpoint.passed:
            return report.endpoint, "network/endpoint path unhealthy"
        return None
