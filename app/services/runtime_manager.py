from __future__ import annotations

from dataclasses import dataclass, replace
import threading
from typing import Callable

from app.domain.errors import RABError
from app.domain.supervisor import SupervisorSnapshot, SupervisorState, utc_now
from app.infrastructure.profile_store import ProfileLockError, ProfileStore
from app.services.local_proxy import LocalProxyService
from app.services.profile_service import ProfileService
from app.services.tunnel import TunnelManager
from app.services.tunnel_supervisor import SupervisorPolicy, TunnelSupervisor


@dataclass(frozen=True)
class RuntimeStopResult:
    profile_name: str
    stop_requested: bool
    worker_exited: bool
    snapshot: SupervisorSnapshot


@dataclass(frozen=True)
class RuntimeShutdownResult:
    results: tuple[RuntimeStopResult, ...]

    @property
    def completed(self) -> bool:
        return all(
            result.worker_exited and result.snapshot.state is SupervisorState.STOPPED
            for result in self.results
        )


@dataclass
class _Worker:
    supervisor: TunnelSupervisor
    stop_event: threading.Event
    finished_event: threading.Event
    thread: threading.Thread


SupervisorFactory = Callable[[str], TunnelSupervisor]


class RuntimeManager:
    """Process-local registry and lifecycle owner for profile supervisors."""

    def __init__(
        self,
        profiles: ProfileService,
        store: ProfileStore,
        local_proxy: LocalProxyService,
        tunnel_manager: TunnelManager,
        *,
        policy: SupervisorPolicy | None = None,
        shutdown_timeout: float = 10.0,
        supervisor_factory: SupervisorFactory | None = None,
    ) -> None:
        self.profiles = profiles
        self.store = store
        self.local_proxy = local_proxy
        self.tunnel_manager = tunnel_manager
        self.policy = policy or SupervisorPolicy()
        self.shutdown_timeout = shutdown_timeout
        self._registry_lock = threading.RLock()
        self._workers: dict[str, _Worker] = {}
        self._stopping_profiles: set[str] = set()
        self._closed = False
        self._supervisor_factory = supervisor_factory or self._new_supervisor

    def start(self, name: str) -> SupervisorSnapshot:
        with self._registry_lock:
            self._ensure_start_allowed(name)
        self.profiles.get(name)
        with self._registry_lock:
            self._ensure_start_allowed(name)
            current = self._workers.get(name)
            if current is not None and not current.finished_event.is_set():
                return current.supervisor.snapshot()

            supervisor = self._supervisor_factory(name)
            stop_event = threading.Event()
            finished_event = threading.Event()
            thread = threading.Thread(
                target=self._run_worker,
                args=(supervisor, stop_event, finished_event),
                name=f"rab-supervisor-{name}",
                daemon=False,
            )
            worker = _Worker(supervisor, stop_event, finished_event, thread)
            self._workers[name] = worker
            thread.start()
            return supervisor.snapshot()

    def stop(self, name: str, timeout: float | None = None) -> RuntimeStopResult:
        with self._registry_lock:
            self._begin_profile_stop(name)
            worker = self._workers.get(name)
            if worker is not None and not worker.finished_event.is_set():
                worker.stop_event.set()
                thread = worker.thread
            else:
                thread = None

        if thread is not None:
            try:
                worker_exited = worker.finished_event.wait(
                    self.shutdown_timeout if timeout is None else timeout
                )
                if worker_exited:
                    thread.join(0)
                snapshot = worker.supervisor.snapshot()
                if worker_exited:
                    snapshot = replace(snapshot, supervised=False)
                return RuntimeStopResult(name, True, worker_exited, snapshot)
            finally:
                with self._registry_lock:
                    self._stopping_profiles.discard(name)
        return self._stop_unsupervised_guarded(name, worker)

    def status(self, name: str) -> SupervisorSnapshot:
        with self._registry_lock:
            worker = self._workers.get(name)
            if worker is not None:
                snapshot = worker.supervisor.snapshot()
                if not worker.finished_event.is_set():
                    return replace(snapshot, supervised=True)
                return replace(snapshot, supervised=False)

        self.profiles.get(name)
        state = self.store.load_runtime(name)
        if state is None:
            return SupervisorSnapshot(
                profile_name=name,
                state=SupervisorState.UNSUPERVISED,
                updated_at=utc_now(),
                message="profile is not supervised and has no runtime evidence",
                supervised=False,
            )
        process_alive = self.tunnel_manager.inspector.matches(state)
        return SupervisorSnapshot(
            profile_name=name,
            state=SupervisorState.UNSUPERVISED,
            updated_at=utc_now(),
            message=(
                "runtime evidence exists and the owned process identity matches"
                if process_alive
                else "runtime evidence exists but the process identity is absent or mismatched"
            ),
            pid=state.pid,
            tunnel_id=state.tunnel_id,
            remote_port=state.remote_port,
            last_successful_probe_at=state.last_successful_probe_at,
            supervised=False,
            runtime_present=True,
            process_alive=process_alive,
        )

    def list_status(self) -> tuple[SupervisorSnapshot, ...]:
        names = {profile.name for profile in self.profiles.list()}
        with self._registry_lock:
            names.update(self._workers)
        return tuple(self.status(name) for name in sorted(names))

    def shutdown(self, timeout: float | None = None) -> RuntimeShutdownResult:
        with self._registry_lock:
            self._closed = True
            workers = tuple(self._workers.items())
            for _, worker in workers:
                if not worker.finished_event.is_set():
                    worker.stop_event.set()

        limit = self.shutdown_timeout if timeout is None else timeout
        results: list[RuntimeStopResult] = []
        for name, worker in workers:
            if not worker.finished_event.is_set():
                worker.finished_event.wait(limit)
            if not worker.finished_event.is_set():
                results.append(RuntimeStopResult(name, True, False, worker.supervisor.snapshot()))
                continue
            worker.thread.join(0)

            snapshot = replace(worker.supervisor.snapshot(), supervised=False)
            try:
                runtime_exists = self.store.load_runtime(name) is not None
            except Exception as exc:
                results.append(
                    RuntimeStopResult(
                        name,
                        True,
                        True,
                        SupervisorSnapshot(
                            profile_name=name,
                            state=SupervisorState.FAILED,
                            updated_at=utc_now(),
                            error_code="RUNTIME_STATE_UNAVAILABLE",
                            message=f"could not inspect runtime during shutdown: {exc}",
                            supervised=False,
                        ),
                    )
                )
                continue
            if snapshot.state is SupervisorState.STOPPED and not runtime_exists:
                results.append(RuntimeStopResult(name, True, True, snapshot))
                continue
            results.append(self._shutdown_unsupervised(name, worker, runtime_exists))
        return RuntimeShutdownResult(tuple(results))

    def wait(self, name: str, timeout: float | None = None) -> SupervisorSnapshot:
        with self._registry_lock:
            worker = self._workers.get(name)
        if worker is None:
            return self.status(name)
        worker.finished_event.wait(timeout)
        return self.status(name)

    def _new_supervisor(self, name: str) -> TunnelSupervisor:
        return TunnelSupervisor(
            name,
            self.store,
            self.local_proxy,
            self.tunnel_manager,
            self.policy,
        )

    def _ensure_start_allowed(self, name: str) -> None:
        if self._closed:
            raise RABError(
                "RUNTIME_MANAGER_SHUTTING_DOWN",
                "runtime manager is shutting down and cannot start new supervisors",
            )
        if name in self._stopping_profiles:
            raise RABError(
                "RUNTIME_STOP_IN_PROGRESS",
                f"runtime stop is already in progress for profile '{name}'",
                retryable=True,
                details={"name": name},
            )

    def _begin_profile_stop(self, name: str) -> None:
        if name in self._stopping_profiles:
            raise RABError(
                "RUNTIME_STOP_IN_PROGRESS",
                f"runtime stop is already in progress for profile '{name}'",
                retryable=True,
                details={"name": name},
            )
        self._stopping_profiles.add(name)

    def _stop_unsupervised_guarded(
        self,
        name: str,
        expected_worker: _Worker | None,
    ) -> RuntimeStopResult:
        try:
            return self._stop_unsupervised(name)
        finally:
            with self._registry_lock:
                current = self._workers.get(name)
                if current is expected_worker and current is not None and current.finished_event.is_set():
                    self._workers.pop(name, None)
                self._stopping_profiles.discard(name)

    def _shutdown_unsupervised(
        self,
        name: str,
        expected_worker: _Worker,
        runtime_exists: bool,
    ) -> RuntimeStopResult:
        with self._registry_lock:
            try:
                self._begin_profile_stop(name)
            except RABError as exc:
                return RuntimeStopResult(
                    name,
                    True,
                    True,
                    SupervisorSnapshot(
                        profile_name=name,
                        state=SupervisorState.FAILED,
                        updated_at=utc_now(),
                        error_code=exc.code,
                        message=exc.message,
                        supervised=False,
                        runtime_present=runtime_exists,
                    ),
                )
        try:
            return self._stop_unsupervised_guarded(name, expected_worker)
        except RABError as exc:
            return RuntimeStopResult(
                name,
                True,
                True,
                SupervisorSnapshot(
                    profile_name=name,
                    state=SupervisorState.FAILED,
                    updated_at=utc_now(),
                    error_code=exc.code,
                    message=exc.message,
                    supervised=False,
                    runtime_present=runtime_exists,
                ),
            )

    def _stop_unsupervised(self, name: str) -> RuntimeStopResult:
        self.profiles.get(name)
        try:
            with self.store.supervisor_lock(name):
                state = self.store.load_runtime(name)
                if state is None:
                    snapshot = SupervisorSnapshot(
                        profile_name=name,
                        state=SupervisorState.STOPPED,
                        updated_at=utc_now(),
                        message="already stopped; no runtime evidence exists",
                        supervised=False,
                    )
                    return RuntimeStopResult(name, False, True, snapshot)

                if not self.tunnel_manager.inspector.matches(state):
                    snapshot = SupervisorSnapshot(
                        profile_name=name,
                        state=SupervisorState.FAILED,
                        updated_at=utc_now(),
                        error_code="PROCESS_IDENTITY_MISMATCH",
                        message=(
                            "recorded process identity is absent or mismatched; "
                            "no process was terminated and runtime evidence was retained"
                        ),
                        pid=state.pid,
                        tunnel_id=state.tunnel_id,
                        remote_port=state.remote_port,
                        last_successful_probe_at=state.last_successful_probe_at,
                        supervised=False,
                        runtime_present=True,
                        process_alive=False,
                    )
                    return RuntimeStopResult(name, True, True, snapshot)

                try:
                    result = self.tunnel_manager.disconnect(name)
                except Exception as exc:
                    if self.store.load_runtime(name) is None:
                        self.store.save_runtime(state)
                    snapshot = SupervisorSnapshot(
                        profile_name=name,
                        state=SupervisorState.FAILED,
                        updated_at=utc_now(),
                        error_code="PROCESS_STOP_FAILED",
                        message=f"owned process disconnect failed: {exc}",
                        pid=state.pid,
                        tunnel_id=state.tunnel_id,
                        remote_port=state.remote_port,
                        last_successful_probe_at=state.last_successful_probe_at,
                        supervised=False,
                        runtime_present=True,
                        process_alive=self.tunnel_manager.inspector.matches(state),
                    )
                    return RuntimeStopResult(name, True, True, snapshot)
                if result.passed:
                    snapshot = SupervisorSnapshot(
                        profile_name=name,
                        state=SupervisorState.STOPPED,
                        updated_at=utc_now(),
                        message=result.detail,
                        supervised=False,
                    )
                    return RuntimeStopResult(name, True, True, snapshot)

                if self.store.load_runtime(name) is None:
                    self.store.save_runtime(state)
                snapshot = SupervisorSnapshot(
                    profile_name=name,
                    state=SupervisorState.FAILED,
                    updated_at=utc_now(),
                    error_code=result.error_code or "PROCESS_STOP_FAILED",
                    message=result.detail,
                    pid=state.pid,
                    tunnel_id=state.tunnel_id,
                    remote_port=state.remote_port,
                    last_successful_probe_at=state.last_successful_probe_at,
                    supervised=False,
                    runtime_present=True,
                    process_alive=self.tunnel_manager.inspector.matches(state),
                )
                return RuntimeStopResult(name, True, True, snapshot)
        except ProfileLockError as exc:
            snapshot = SupervisorSnapshot(
                profile_name=name,
                state=SupervisorState.FAILED,
                updated_at=utc_now(),
                error_code="PROFILE_BUSY",
                message=str(exc),
                supervised=False,
                runtime_present=self.store.load_runtime(name) is not None,
            )
            return RuntimeStopResult(name, True, True, snapshot)

    @staticmethod
    def _run_worker(
        supervisor: TunnelSupervisor,
        stop_event: threading.Event,
        finished_event: threading.Event,
    ) -> None:
        try:
            supervisor.run(stop_event)
        except Exception as exc:
            supervisor.fail_unexpected(exc)
        finally:
            finished_event.set()
