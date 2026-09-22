from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import threading
import time
from unittest.mock import MagicMock

import pytest

from app.domain.errors import RABError
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.domain.supervisor import SupervisorSnapshot, SupervisorState, utc_now
from app.infrastructure.profile_store import ProfileStore
from app.services.local_proxy import LocalProxyReport
from app.services.profile_service import ProfileService
from app.services.runtime_manager import RuntimeManager, RuntimeShutdownResult, RuntimeStopResult
from app.services.tunnel import TunnelManager
from app.services.tunnel_supervisor import SupervisorPolicy
from .phase24_helpers import FakeProfiles, runtime_state


def passed(name: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASS, "ok")


class FakeSupervisor:
    def __init__(self, name: str, *, terminal: SupervisorState | None = None, ignore_stop: bool = False) -> None:
        self.name = name
        self.terminal = terminal
        self.ignore_stop = ignore_stop
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._snapshot = self._make(SupervisorState.STARTING, "starting")
        self.run_calls = 0

    def _make(self, state, message):
        return SupervisorSnapshot(self.name, state, utc_now(), message=message)

    def snapshot(self):
        with self._lock:
            return self._snapshot

    def _set(self, state, message):
        with self._lock:
            self._snapshot = self._make(state, message)

    def run(self, stop_event):
        self.run_calls += 1
        self._set(SupervisorState.READY, "ready")
        self.started.set()
        if self.terminal is not None:
            self._set(self.terminal, self.terminal.value.lower())
            return self.snapshot()
        if self.ignore_stop:
            self.release.wait(1)
        else:
            stop_event.wait(1)
        self._set(SupervisorState.STOPPED, "stopped")
        return self.snapshot()

    def fail_unexpected(self, exc):
        self._set(SupervisorState.FAILED, str(exc))
        return self.snapshot()


def manager_with_fakes(tmp_path, names=("server",), factory=None):
    profiles = FakeProfiles(*names)
    store = ProfileStore(tmp_path)
    local = MagicMock()
    tunnel = MagicMock()
    tunnel.inspector.matches.return_value = True
    created: list[FakeSupervisor] = []

    def default_factory(name):
        supervisor = FakeSupervisor(name)
        created.append(supervisor)
        return supervisor

    manager = RuntimeManager(
        profiles,
        store,
        local,
        tunnel,
        shutdown_timeout=0.1,
        supervisor_factory=factory or default_factory,
    )
    return manager, created, profiles, store, tunnel


def wait_started(supervisor: FakeSupervisor):
    assert supervisor.started.wait(0.5)


def test_start_one_profile_creates_non_daemon_worker(tmp_path):
    manager, created, _, _, _ = manager_with_fakes(tmp_path)
    manager.start("server")
    wait_started(created[0])

    assert created[0].run_calls == 1
    assert manager._workers["server"].thread.daemon is False
    manager.stop("server")


def test_duplicate_concurrent_start_is_idempotent(tmp_path):
    manager, created, _, _, _ = manager_with_fakes(tmp_path)
    barrier = threading.Barrier(3)

    def start():
        barrier.wait()
        manager.start("server")

    callers = [threading.Thread(target=start) for _ in range(2)]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(0.5)
    wait_started(created[0])

    assert len(created) == 1
    assert created[0].run_calls == 1
    manager.stop("server")


def test_two_profiles_run_independently(tmp_path):
    manager, created, _, _, _ = manager_with_fakes(tmp_path, ("server-a", "server-b"))
    manager.start("server-a")
    manager.start("server-b")
    for supervisor in created:
        wait_started(supervisor)

    assert {item.name for item in created} == {"server-a", "server-b"}
    assert all(manager._workers[name].thread.is_alive() for name in ("server-a", "server-b"))
    manager.shutdown()


def test_stopping_one_profile_does_not_affect_another(tmp_path):
    manager, created, _, _, _ = manager_with_fakes(tmp_path, ("server-a", "server-b"))
    manager.start("server-a")
    manager.start("server-b")
    for supervisor in created:
        wait_started(supervisor)

    result = manager.stop("server-a")

    assert result.worker_exited
    assert manager._workers["server-b"].thread.is_alive()
    assert manager.status("server-b").state is SupervisorState.READY
    manager.stop("server-b")


def test_stop_already_stopped_is_idempotent(tmp_path):
    manager, created, _, _, _ = manager_with_fakes(tmp_path)
    manager.start("server")
    wait_started(created[0])
    first = manager.stop("server")
    second = manager.stop("server")

    assert first.worker_exited and second.worker_exited
    assert second.snapshot.state is SupervisorState.STOPPED


@pytest.mark.parametrize("terminal", [SupervisorState.FAILED, SupervisorState.STOPPED])
def test_terminal_worker_can_be_started_again(tmp_path, terminal):
    modes = iter([terminal, None])
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, terminal=next(modes))
        created.append(supervisor)
        return supervisor

    manager, _, _, _, _ = manager_with_fakes(tmp_path, factory=factory)
    manager.start("server")
    manager.wait("server", 0.5)
    manager.start("server")
    wait_started(created[1])

    assert len(created) == 2
    manager.stop("server")


def test_status_snapshot_is_safe_under_concurrent_reads(tmp_path):
    manager, created, _, _, _ = manager_with_fakes(tmp_path)
    manager.start("server")
    wait_started(created[0])
    results = []
    threads = [threading.Thread(target=lambda: results.append(manager.status("server"))) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(0.5)

    assert len(results) == 20
    assert all(item.state is SupervisorState.READY for item in results)
    manager.stop("server")


def test_list_status_returns_immutable_isolated_snapshots(tmp_path):
    manager, created, _, _, _ = manager_with_fakes(tmp_path, ("server-a", "server-b"))
    manager.start("server-a")
    wait_started(created[0])

    snapshots = manager.list_status()

    assert isinstance(snapshots, tuple)
    assert [item.profile_name for item in snapshots] == ["server-a", "server-b"]
    with pytest.raises(Exception):
        snapshots[0].message = "changed"
    manager.stop("server-a")


def test_shutdown_requests_and_joins_all_workers(tmp_path):
    manager, created, _, _, _ = manager_with_fakes(tmp_path, ("server-a", "server-b"))
    manager.start("server-a")
    manager.start("server-b")
    for supervisor in created:
        wait_started(supervisor)

    result = manager.shutdown()

    assert result.completed
    assert len(result.results) == 2
    assert all(item.snapshot.state is SupervisorState.STOPPED for item in result.results)


def test_one_shutdown_timeout_does_not_block_stop_signal_to_others(tmp_path):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, ignore_stop=name == "server-a")
        created.append(supervisor)
        return supervisor

    manager, _, _, _, _ = manager_with_fakes(tmp_path, ("server-a", "server-b"), factory)
    manager.start("server-a")
    manager.start("server-b")
    for supervisor in created:
        wait_started(supervisor)

    result = manager.shutdown(timeout=0.02)

    assert not result.completed
    assert manager._workers["server-b"].stop_event.is_set()
    assert manager.status("server-b").state is SupervisorState.STOPPED
    created[0].release.set()
    manager._workers["server-a"].thread.join(0.5)


def test_unknown_profile_creates_no_worker(tmp_path):
    manager, _, _, _, _ = manager_with_fakes(tmp_path)

    with pytest.raises(RABError) as caught:
        manager.start("missing")

    assert caught.value.code == "PROFILE_NOT_FOUND"
    assert "missing" not in manager._workers


def real_manager_with_runtime(tmp_path, *, identity_matches: bool):
    store = ProfileStore(tmp_path)
    profile = Profile(1, "server", "server")
    store.save_profile(profile)
    state = runtime_state()
    store.save_runtime(state)
    runner = MagicMock()
    inspector = MagicMock()
    inspector.matches.return_value = identity_matches
    inspector.terminate_owned.return_value = True
    remote = MagicMock()
    remote.check_listener.return_value = passed("Listener")
    remote.check_endpoint.return_value = passed("Endpoint")
    remote.find_free_port.return_value = 17891
    tunnel = TunnelManager(runner, inspector, store, remote, "ssh.exe", startup_timeout=0)
    profiles = ProfileService(store, tunnel)
    local = MagicMock()
    local.inspect.return_value = LocalProxyReport(passed("TCP"), passed("CONNECT"), passed("Endpoint"))
    manager = RuntimeManager(
        profiles,
        store,
        local,
        tunnel,
        policy=SupervisorPolicy((0.0,), 30.0, 0.0),
        shutdown_timeout=0.2,
    )
    return manager, store, runner, inspector


def test_new_manager_adopts_matching_existing_runtime_without_new_process(tmp_path):
    manager, _, runner, _ = real_manager_with_runtime(tmp_path, identity_matches=True)
    manager.start("server")
    deadline = time.monotonic() + 0.5
    while manager.status("server").state is not SupervisorState.READY and time.monotonic() < deadline:
        time.sleep(0.005)

    assert manager.status("server").state is SupervisorState.READY
    runner.start_managed.assert_not_called()
    manager.stop("server")


def test_mismatched_runtime_never_blindly_terminates_process(tmp_path):
    manager, store, runner, inspector = real_manager_with_runtime(tmp_path, identity_matches=False)
    manager.start("server")
    manager.wait("server", 0.5)

    assert manager.status("server").error_code == "REMOTE_PORT_CONFLICT"
    assert store.load_runtime("server") is not None
    inspector.terminate_owned.assert_not_called()
    runner.start_managed.assert_not_called()


@pytest.mark.parametrize("identity_matches", [True, False])
def test_status_without_worker_reports_unsupervised_runtime_evidence(tmp_path, identity_matches):
    manager, _, _, _ = real_manager_with_runtime(tmp_path, identity_matches=identity_matches)

    snapshot = manager.status("server")

    assert snapshot.state is SupervisorState.UNSUPERVISED
    assert snapshot.supervised is False
    assert snapshot.runtime_present is True
    assert snapshot.process_alive is identity_matches
    assert snapshot.pid == 123


def test_worker_reads_authoritative_profile_only_after_supervisor_lock(tmp_path):
    store = ProfileStore(tmp_path)
    initial = Profile(1, "server", "server", local_proxy_port=7890)
    store.save_profile(initial)
    tunnel = MagicMock()
    tunnel.stop_timeout = 0.01
    tunnel.inspector.matches.return_value = True
    active = MagicMock()
    active.state = runtime_state()
    active.poll.return_value = None
    active.stop.return_value = True

    def acquire(selected_profile):
        store.save_runtime(active.state)
        return active

    tunnel.acquire.side_effect = acquire
    tunnel.verify.return_value = (passed("Listener"), passed("Endpoint"))
    profiles = ProfileService(store, tunnel)
    local = MagicMock()
    local.inspect.return_value = LocalProxyReport(passed("TCP"), passed("CONNECT"), passed("Endpoint"))
    entered = threading.Event()
    release = threading.Event()
    original_lock = store.supervisor_lock

    @contextmanager
    def gated_lock(name):
        if threading.current_thread().name == "rab-supervisor-server":
            entered.set()
            assert release.wait(0.5)
        with original_lock(name):
            yield

    store.supervisor_lock = gated_lock
    manager = RuntimeManager(
        profiles,
        store,
        local,
        tunnel,
        policy=SupervisorPolicy((0.0,), 30.0, 0.0),
    )

    manager.start("server")
    assert entered.wait(0.5)
    profiles.update("server", {"local_proxy_port": 7897})
    release.set()
    deadline = time.monotonic() + 0.5
    while manager.status("server").state is not SupervisorState.READY and time.monotonic() < deadline:
        time.sleep(0.005)

    assert manager.status("server").state is SupervisorState.READY
    assert local.inspect.call_args.args[0].local_proxy_port == 7897
    assert tunnel.acquire.call_args.args[0].local_proxy_port == 7897
    manager.stop("server")


def test_profile_deleted_between_start_and_lock_never_touches_network(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_profile(Profile(1, "server", "server"))
    tunnel = MagicMock()
    profiles = ProfileService(store, tunnel)
    local = MagicMock()
    entered = threading.Event()
    release = threading.Event()
    original_lock = store.supervisor_lock

    @contextmanager
    def gated_lock(name):
        if threading.current_thread().name == "rab-supervisor-server":
            entered.set()
            assert release.wait(0.5)
        with original_lock(name):
            yield

    store.supervisor_lock = gated_lock
    manager = RuntimeManager(profiles, store, local, tunnel)

    manager.start("server")
    assert entered.wait(0.5)
    profiles.delete("server")
    release.set()
    manager.wait("server", 0.5)

    snapshot = manager.status("server")
    assert snapshot.state is SupervisorState.FAILED
    assert snapshot.error_code == "PROFILE_NOT_FOUND"
    local.inspect.assert_not_called()
    tunnel.acquire.assert_not_called()


def test_stop_without_worker_or_runtime_is_idempotently_stopped(tmp_path):
    manager, _, _, _, tunnel = manager_with_fakes(tmp_path)

    result = manager.stop("server")

    assert result.worker_exited
    assert result.stop_requested is False
    assert result.snapshot.state is SupervisorState.STOPPED
    tunnel.disconnect.assert_not_called()


def test_stop_without_worker_disconnects_matching_owned_runtime(tmp_path):
    manager, _, _, store, tunnel = manager_with_fakes(tmp_path)
    state = runtime_state()
    store.save_runtime(state)
    tunnel.inspector.matches.return_value = True

    def disconnect(name):
        store.clear_runtime(name)
        return passed("Tunnel process")

    tunnel.disconnect.side_effect = disconnect

    result = manager.stop("server")

    assert result.snapshot.state is SupervisorState.STOPPED
    assert store.load_runtime("server") is None
    tunnel.disconnect.assert_called_once_with("server")


def test_stop_without_worker_preserves_mismatched_runtime(tmp_path):
    manager, _, _, store, tunnel = manager_with_fakes(tmp_path)
    state = runtime_state()
    store.save_runtime(state)
    tunnel.inspector.matches.return_value = False

    result = manager.stop("server")

    assert result.snapshot.state is SupervisorState.FAILED
    assert result.snapshot.error_code == "PROCESS_IDENTITY_MISMATCH"
    assert result.snapshot.runtime_present is True
    assert store.load_runtime("server") == state
    tunnel.disconnect.assert_not_called()
    tunnel.inspector.terminate_owned.assert_not_called()


def test_stop_without_worker_preserves_runtime_if_disconnect_raises(tmp_path):
    manager, _, _, store, tunnel = manager_with_fakes(tmp_path)
    state = runtime_state()
    store.save_runtime(state)
    tunnel.inspector.matches.return_value = True
    tunnel.disconnect.side_effect = RuntimeError("password=do-not-leak")

    result = manager.stop("server")

    assert result.snapshot.state is SupervisorState.FAILED
    assert result.snapshot.error_code == "PROCESS_STOP_FAILED"
    assert "do-not-leak" not in result.snapshot.message
    assert store.load_runtime("server") == state


def test_stop_without_worker_returns_profile_busy_without_termination(tmp_path):
    manager, _, _, store, tunnel = manager_with_fakes(tmp_path)
    store.save_runtime(runtime_state())

    with store.supervisor_lock("server"):
        result = manager.stop("server")

    assert result.snapshot.state is SupervisorState.FAILED
    assert result.snapshot.error_code == "PROFILE_BUSY"
    assert store.load_runtime("server") is not None
    tunnel.disconnect.assert_not_called()
    tunnel.inspector.terminate_owned.assert_not_called()


def test_restarted_manager_can_stop_unsupervised_matching_process(tmp_path):
    manager, store, _, inspector = real_manager_with_runtime(tmp_path, identity_matches=True)

    before = manager.status("server")
    result = manager.stop("server")

    assert before.state is SupervisorState.UNSUPERVISED
    assert before.process_alive is True
    assert result.snapshot.state is SupervisorState.STOPPED
    inspector.terminate_owned.assert_called_once()
    assert store.load_runtime("server") is None


@pytest.mark.parametrize(
    ("terminal", "expected_supervised"),
    [
        (None, True),
        (SupervisorState.STOPPED, False),
        (SupervisorState.FAILED, False),
    ],
)
def test_status_supervised_flag_reflects_live_worker_only(tmp_path, terminal, expected_supervised):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, terminal=terminal)
        created.append(supervisor)
        return supervisor

    manager, _, _, _, _ = manager_with_fakes(tmp_path, factory=factory)
    manager.start("server")
    wait_started(created[0])
    if terminal is not None:
        manager.wait("server", 0.5)

    snapshot = manager.status("server")

    assert snapshot.supervised is expected_supervised
    assert snapshot.state is (SupervisorState.READY if terminal is None else terminal)
    if terminal is None:
        manager.stop("server")


def test_list_status_marks_dead_worker_unsupervised_without_losing_terminal_state(tmp_path):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, terminal=SupervisorState.FAILED)
        created.append(supervisor)
        return supervisor

    manager, _, _, _, _ = manager_with_fakes(tmp_path, factory=factory)
    manager.start("server")
    manager.wait("server", 0.5)

    snapshot = manager.list_status()[0]

    assert snapshot.state is SupervisorState.FAILED
    assert snapshot.supervised is False
    assert snapshot.message == "failed"


def test_shutdown_completed_requires_stopped_snapshot_even_if_worker_exited():
    failed = SupervisorSnapshot("server", SupervisorState.FAILED, utc_now(), supervised=False)
    result = RuntimeShutdownResult((RuntimeStopResult("server", True, True, failed),))

    assert result.completed is False


def test_shutdown_cleans_matching_runtime_left_by_dead_failed_worker(tmp_path):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, terminal=SupervisorState.FAILED)
        created.append(supervisor)
        return supervisor

    manager, _, _, store, tunnel = manager_with_fakes(tmp_path, factory=factory)
    manager.start("server")
    manager.wait("server", 0.5)
    store.save_runtime(runtime_state())
    tunnel.inspector.matches.return_value = True

    def disconnect(name):
        store.clear_runtime(name)
        return passed("Tunnel process")

    tunnel.disconnect.side_effect = disconnect

    result = manager.shutdown()

    assert result.completed is True
    assert result.results[0].snapshot.state is SupervisorState.STOPPED
    assert result.results[0].snapshot.supervised is False
    assert store.load_runtime("server") is None
    tunnel.disconnect.assert_called_once_with("server")


def test_shutdown_preserves_mismatched_runtime_left_by_dead_failed_worker(tmp_path):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, terminal=SupervisorState.FAILED)
        created.append(supervisor)
        return supervisor

    manager, _, _, store, tunnel = manager_with_fakes(tmp_path, factory=factory)
    manager.start("server")
    manager.wait("server", 0.5)
    state = runtime_state()
    store.save_runtime(state)
    tunnel.inspector.matches.return_value = False

    result = manager.shutdown()

    assert result.completed is False
    assert result.results[0].snapshot.state is SupervisorState.FAILED
    assert result.results[0].snapshot.error_code == "PROCESS_IDENTITY_MISMATCH"
    assert store.load_runtime("server") == state
    tunnel.disconnect.assert_not_called()
    tunnel.inspector.terminate_owned.assert_not_called()


def test_shutdown_closes_manager_against_concurrent_and_later_start(tmp_path):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, ignore_stop=name == "server-a")
        created.append(supervisor)
        return supervisor

    manager, _, _, _, _ = manager_with_fakes(tmp_path, ("server-a", "server-b"), factory)
    manager.start("server-a")
    wait_started(created[0])
    shutdown_result = []
    shutdown_thread = threading.Thread(target=lambda: shutdown_result.append(manager.shutdown(timeout=0.5)))
    shutdown_thread.start()
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        with manager._registry_lock:
            if manager._closed:
                break
        time.sleep(0.005)

    with pytest.raises(RABError) as during:
        manager.start("server-b")
    assert during.value.code == "RUNTIME_MANAGER_SHUTTING_DOWN"
    assert "server-b" not in manager._workers

    created[0].release.set()
    shutdown_thread.join(0.5)
    assert not shutdown_thread.is_alive()
    assert shutdown_result[0].completed is True

    with pytest.raises(RABError) as after:
        manager.start("server-b")
    assert after.value.code == "RUNTIME_MANAGER_SHUTTING_DOWN"


def test_dead_failed_matching_stop_removes_stale_worker_before_status(tmp_path):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, terminal=SupervisorState.FAILED)
        created.append(supervisor)
        return supervisor

    manager, _, _, store, tunnel = manager_with_fakes(tmp_path, factory=factory)
    manager.start("server")
    manager.wait("server", 0.5)
    store.save_runtime(runtime_state())
    tunnel.inspector.matches.return_value = True

    def disconnect(name):
        store.clear_runtime(name)
        return passed("Tunnel process")

    tunnel.disconnect.side_effect = disconnect

    result = manager.stop("server")
    current = manager.status("server")

    assert result.snapshot.state is SupervisorState.STOPPED
    assert "server" not in manager._workers
    assert current.state is SupervisorState.UNSUPERVISED
    assert current.runtime_present is False
    assert current.state is not SupervisorState.FAILED


def test_dead_failed_mismatch_stop_status_reflects_runtime_evidence(tmp_path):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, terminal=SupervisorState.FAILED)
        created.append(supervisor)
        return supervisor

    manager, _, _, store, tunnel = manager_with_fakes(tmp_path, factory=factory)
    manager.start("server")
    manager.wait("server", 0.5)
    state = runtime_state()
    store.save_runtime(state)
    tunnel.inspector.matches.return_value = False

    result = manager.stop("server")
    current = manager.status("server")

    assert result.snapshot.error_code == "PROCESS_IDENTITY_MISMATCH"
    assert current.state is SupervisorState.UNSUPERVISED
    assert current.runtime_present is True
    assert current.process_alive is False
    assert current.pid == state.pid
    assert store.load_runtime("server") == state


def blocking_disconnect(store, entered, release):
    def disconnect(name):
        entered.set()
        assert release.wait(0.5)
        store.clear_runtime(name)
        return passed("Tunnel process")

    return disconnect


def test_unsupervised_stop_blocks_concurrent_start_of_same_profile(tmp_path):
    manager, created, _, store, tunnel = manager_with_fakes(tmp_path)
    store.save_runtime(runtime_state())
    tunnel.inspector.matches.return_value = True
    entered = threading.Event()
    release = threading.Event()
    tunnel.disconnect.side_effect = blocking_disconnect(store, entered, release)
    stop_results = []
    stop_thread = threading.Thread(target=lambda: stop_results.append(manager.stop("server")))
    stop_thread.start()
    assert entered.wait(0.5)

    with pytest.raises(RABError) as caught:
        manager.start("server")

    assert caught.value.code == "RUNTIME_STOP_IN_PROGRESS"
    assert created == []
    release.set()
    stop_thread.join(0.5)
    assert stop_results[0].snapshot.state is SupervisorState.STOPPED


def test_stop_in_progress_for_one_profile_does_not_block_other_start(tmp_path):
    manager, created, _, store, tunnel = manager_with_fakes(tmp_path, ("server-a", "server-b"))
    state = replace(runtime_state(), profile_name="server-a")
    store.save_runtime(state)
    tunnel.inspector.matches.return_value = True
    entered = threading.Event()
    release = threading.Event()
    tunnel.disconnect.side_effect = blocking_disconnect(store, entered, release)
    stop_thread = threading.Thread(target=lambda: manager.stop("server-a"))
    stop_thread.start()
    assert entered.wait(0.5)

    manager.start("server-b")

    wait_started(created[0])
    assert created[0].name == "server-b"
    release.set()
    stop_thread.join(0.5)
    manager.stop("server-b")


def test_duplicate_unsupervised_stop_never_duplicates_disconnect(tmp_path):
    manager, _, _, store, tunnel = manager_with_fakes(tmp_path)
    store.save_runtime(runtime_state())
    tunnel.inspector.matches.return_value = True
    entered = threading.Event()
    release = threading.Event()
    tunnel.disconnect.side_effect = blocking_disconnect(store, entered, release)
    first_results = []
    first = threading.Thread(target=lambda: first_results.append(manager.stop("server")))
    first.start()
    assert entered.wait(0.5)

    with pytest.raises(RABError) as caught:
        manager.stop("server")

    assert caught.value.code == "RUNTIME_STOP_IN_PROGRESS"
    assert tunnel.disconnect.call_count == 1
    release.set()
    first.join(0.5)
    assert first_results[0].snapshot.state is SupervisorState.STOPPED


def test_shutdown_concurrent_with_unsupervised_stop_does_not_duplicate_cleanup(tmp_path):
    created = []

    def factory(name):
        supervisor = FakeSupervisor(name, terminal=SupervisorState.FAILED)
        created.append(supervisor)
        return supervisor

    manager, _, _, store, tunnel = manager_with_fakes(tmp_path, factory=factory)
    manager.start("server")
    manager.wait("server", 0.5)
    store.save_runtime(runtime_state())
    tunnel.inspector.matches.return_value = True
    entered = threading.Event()
    release = threading.Event()
    tunnel.disconnect.side_effect = blocking_disconnect(store, entered, release)
    stop_results = []
    stop_thread = threading.Thread(target=lambda: stop_results.append(manager.stop("server")))
    stop_thread.start()
    assert entered.wait(0.5)

    shutdown_result = manager.shutdown()

    assert shutdown_result.completed is False
    assert shutdown_result.results[0].snapshot.error_code == "RUNTIME_STOP_IN_PROGRESS"
    assert tunnel.disconnect.call_count == 1
    release.set()
    stop_thread.join(0.5)
    assert stop_results[0].snapshot.state is SupervisorState.STOPPED
    assert store.load_runtime("server") is None


def test_wait_polling_uses_finished_event_and_never_thread_join(tmp_path, monkeypatch):
    manager, created, _, _, _ = manager_with_fakes(tmp_path)
    manager.start("server")
    wait_started(created[0])
    worker = manager._workers["server"]

    with monkeypatch.context() as patch:
        patch.setattr(worker.thread, "join", MagicMock(side_effect=AssertionError("join must not poll")))
        snapshot = manager.wait("server", 0.001)

    assert snapshot.state is SupervisorState.READY
    assert snapshot.supervised is True
    manager.stop("server")


def test_interrupted_wait_still_stops_manager_owned_worker_gracefully(tmp_path, monkeypatch):
    manager, store, _, inspector = real_manager_with_runtime(tmp_path, identity_matches=True)
    disconnect = MagicMock(wraps=manager.tunnel_manager.disconnect)
    manager.tunnel_manager.disconnect = disconnect
    manager.start("server")
    deadline = time.monotonic() + 0.5
    while manager.status("server").state is not SupervisorState.READY and time.monotonic() < deadline:
        time.sleep(0.005)
    worker = manager._workers["server"]

    with monkeypatch.context() as patch:
        patch.setattr(worker.finished_event, "wait", MagicMock(side_effect=KeyboardInterrupt))
        with pytest.raises(KeyboardInterrupt):
            manager.wait("server", 0.25)

    # Reproduce the misleading Thread state observed after an interrupted
    # Windows join. RuntimeManager must trust finished_event, not is_alive().
    with monkeypatch.context() as patch:
        patch.setattr(worker.thread, "is_alive", MagicMock(return_value=False))
        result = manager.stop("server")

    assert worker.stop_event.is_set()
    assert worker.finished_event.is_set()
    assert result.worker_exited is True
    assert result.snapshot.state is SupervisorState.STOPPED
    assert result.snapshot.error_code is None
    assert store.load_runtime("server") is None
    inspector.terminate_owned.assert_called_once()
    disconnect.assert_not_called()
