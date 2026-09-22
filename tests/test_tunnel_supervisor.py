from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from app.domain.health import CheckResult, CheckStatus
from app.domain.errors import RABError
from app.domain.profile import Profile, RuntimeState
from app.domain.supervisor import SupervisorState
from app.infrastructure.profile_store import ProfileStore
from app.services.local_proxy import LocalProxyReport
from app.services.profile_service import ProfileService
from app.services.tunnel import RemotePortConflictError, TunnelError
from app.services.tunnel_supervisor import SupervisorPolicy, TunnelSupervisor
from .phase24_helpers import runtime_state


def passed(name: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASS, "ok")


def failed(name: str, code: str = "FAILED") -> CheckResult:
    return CheckResult(name, CheckStatus.FAIL, "failed", code)


def healthy_local() -> LocalProxyReport:
    return LocalProxyReport(passed("TCP"), passed("CONNECT"), passed("Endpoint"))


def local_failure(*, endpoint: bool = False) -> LocalProxyReport:
    if endpoint:
        return LocalProxyReport(passed("TCP"), passed("CONNECT"), failed("Endpoint", "ENDPOINT_UNREACHABLE"))
    return LocalProxyReport(failed("TCP", "LOCAL_PROXY_REFUSED"), failed("CONNECT"), failed("Endpoint"))


def profile(*, auto_reconnect: bool = True) -> Profile:
    return Profile(1, "server", "server", auto_reconnect=auto_reconnect)


class ActiveStub:
    def __init__(self, state: RuntimeState, *, polls=None, stops: bool = True) -> None:
        self.state = state
        self._polls = iter(polls or [])
        self.stops = stops
        self.stop_calls = 0

    def poll(self):
        try:
            return next(self._polls)
        except StopIteration:
            return None

    def stop(self, timeout):
        self.stop_calls += 1
        return self.stops


class ControlledEvent:
    def __init__(self, waits: list[bool]) -> None:
        self.waits = iter(waits)
        self.set_value = False
        self.timeouts: list[float] = []

    def is_set(self):
        return self.set_value

    def wait(self, timeout):
        self.timeouts.append(timeout)
        try:
            result = next(self.waits)
        except StopIteration:
            result = True
        if result:
            self.set_value = True
        return result


def make_supervisor(tmp_path, *, local=None, tunnel=None, backoff=(0.0,)):
    store = ProfileStore(tmp_path)
    store.save_profile(profile())
    local = local or MagicMock()
    local.inspect.return_value = healthy_local()
    tunnel = tunnel or MagicMock()
    tunnel.stop_timeout = 0.01
    tunnel.inspector.matches.return_value = True
    active = ActiveStub(runtime_state())

    def acquire(_profile):
        store.save_runtime(active.state)
        return active

    tunnel.acquire.side_effect = acquire
    tunnel.verify.return_value = (passed("Listener"), passed("Remote endpoint"))
    supervisor = TunnelSupervisor(
        "server",
        store,
        local,
        tunnel,
        SupervisorPolicy(backoff, 0.0, 0.0),
    )
    history = []
    original_publish = supervisor._publish

    def publish(*args, **kwargs):
        snapshot = original_publish(*args, **kwargs)
        history.append(snapshot)
        return snapshot

    supervisor._publish = publish
    return supervisor, store, local, tunnel, active, history


def test_happy_path_reaches_ready_and_stops_safely(tmp_path):
    supervisor, store, _, tunnel, active, history = make_supervisor(tmp_path)
    result = supervisor.run(ControlledEvent([True]))

    assert SupervisorState.READY in [item.state for item in history]
    assert result.state is SupervisorState.STOPPED
    assert tunnel.acquire.call_count == 1
    assert tunnel.verify.call_count == 1
    assert active.stop_calls == 1
    assert store.load_runtime("server") is None


def test_local_tcp_failure_degrades_backs_off_and_retries(tmp_path):
    supervisor, _, local, tunnel, _, history = make_supervisor(tmp_path, backoff=(0.25,))
    local.inspect.side_effect = [local_failure(), healthy_local()]
    event = ControlledEvent([False, True])

    supervisor.run(event)

    degraded = next(item for item in history if item.state is SupervisorState.DEGRADED)
    assert degraded.error_code == "LOCAL_PROXY_REFUSED"
    assert degraded.retry_in_seconds == 0.25
    assert local.inspect.call_count == 2
    assert tunnel.acquire.call_count == 1


def test_endpoint_failure_is_classified_as_network_path_and_retries(tmp_path):
    supervisor, _, local, _, _, history = make_supervisor(tmp_path)
    local.inspect.side_effect = [local_failure(endpoint=True), healthy_local()]

    supervisor.run(ControlledEvent([False, True]))

    degraded = next(item for item in history if item.state is SupervisorState.DEGRADED)
    assert degraded.error_code == "ENDPOINT_UNREACHABLE"
    assert "network/endpoint path" in degraded.message
    assert "local proxy unhealthy" not in degraded.message


def test_auto_reconnect_false_is_terminal_without_loop(tmp_path):
    supervisor, _, local, tunnel, _, _ = make_supervisor(tmp_path)
    local.inspect.return_value = local_failure()

    store = supervisor.store
    store.update_profile(profile(auto_reconnect=False))
    result = supervisor.run(ControlledEvent([]))

    assert result.state is SupervisorState.FAILED
    assert local.inspect.call_count == 1
    tunnel.acquire.assert_not_called()


def test_tunnel_error_retries_when_enabled(tmp_path):
    supervisor, store, _, tunnel, active, history = make_supervisor(tmp_path)

    def acquire(_profile):
        if tunnel.acquire.call_count == 1:
            raise TunnelError("temporary", "TUNNEL_START_FAILED")
        store.save_runtime(active.state)
        return active

    tunnel.acquire.side_effect = acquire

    supervisor.run(ControlledEvent([False, True]))

    assert tunnel.acquire.call_count == 2
    assert any(item.error_code == "TUNNEL_START_FAILED" for item in history)


def test_remote_port_conflict_is_terminal_suggestion_only(tmp_path):
    supervisor, _, _, tunnel, _, _ = make_supervisor(tmp_path)
    tunnel.acquire.side_effect = RemotePortConflictError(17890, 17891)
    original = profile()

    result = supervisor.run(ControlledEvent([]))

    assert result.state is SupervisorState.FAILED
    assert result.error_code == "REMOTE_PORT_CONFLICT"
    assert result.suggested_port == 17891
    assert original.remote_port == 17890


def test_exited_process_retains_runtime_then_reacquires(tmp_path):
    supervisor, store, _, tunnel, _, history = make_supervisor(tmp_path)
    first = ActiveStub(runtime_state(), polls=[255])
    second = ActiveStub(runtime_state())

    def acquire(_profile):
        if tunnel.acquire.call_count == 1:
            store.save_runtime(first.state)
            return first
        assert store.load_runtime("server") == first.state
        store.save_runtime(second.state)
        return second

    tunnel.acquire.side_effect = acquire

    supervisor.run(ControlledEvent([False, False, True]))

    assert tunnel.acquire.call_count == 2
    assert any(item.error_code == "TUNNEL_EXITED" and item.runtime_present for item in history)


def test_health_failure_with_live_process_reuses_same_tunnel(tmp_path):
    supervisor, _, _, tunnel, active, history = make_supervisor(tmp_path)
    tunnel.verify.side_effect = [
        (passed("Listener"), failed("Endpoint", "ENDPOINT_UNREACHABLE")),
        (passed("Listener"), passed("Endpoint")),
    ]

    supervisor.run(ControlledEvent([False, True]))

    assert tunnel.acquire.call_count == 1
    assert tunnel.verify.call_count == 2
    assert active.stop_calls == 1
    assert any(item.state is SupervisorState.DEGRADED for item in history)


def test_ready_resets_backoff_index(tmp_path):
    supervisor, store, _, tunnel, active, history = make_supervisor(tmp_path, backoff=(1.0, 5.0))
    acquire_attempt = 0

    def acquire(_profile):
        nonlocal acquire_attempt
        acquire_attempt += 1
        if acquire_attempt == 1:
            raise TunnelError("temporary", "TUNNEL_START_FAILED")
        store.save_runtime(active.state)
        return active

    tunnel.acquire.side_effect = acquire
    tunnel.verify.side_effect = [
        (passed("Listener"), passed("Endpoint")),
        (passed("Listener"), failed("Endpoint", "ENDPOINT_UNREACHABLE")),
        (passed("Listener"), passed("Endpoint")),
    ]

    supervisor.run(ControlledEvent([False, False, False, True]))

    retry_delays = [item.retry_in_seconds for item in history if item.retry_in_seconds is not None]
    assert retry_delays == [1.0, 1.0]


def test_explicit_stop_from_ready_transitions_through_stopping(tmp_path):
    supervisor, _, _, _, _, history = make_supervisor(tmp_path)

    result = supervisor.run(ControlledEvent([True]))

    states = [item.state for item in history]
    assert states[-3:] == [SupervisorState.READY, SupervisorState.STOPPING, SupervisorState.STOPPED]
    assert result.state is SupervisorState.STOPPED


def test_stop_failure_preserves_runtime_evidence(tmp_path):
    supervisor, store, _, _, active, _ = make_supervisor(tmp_path)
    active.stops = False

    result = supervisor.run(ControlledEvent([True]))

    assert result.state is SupervisorState.FAILED
    assert result.error_code == "PROCESS_STOP_TIMEOUT"
    assert store.load_runtime("server") == active.state


def test_unexpected_exception_is_failed_redacted_and_cleans_owned_tunnel(tmp_path):
    supervisor, store, _, tunnel, active, _ = make_supervisor(tmp_path)
    tunnel.verify.side_effect = RuntimeError("password=hunter2")

    result = supervisor.run(ControlledEvent([]))

    assert result.state is SupervisorState.FAILED
    assert result.error_code == "SUPERVISOR_FAILED"
    assert "hunter2" not in result.message
    assert "[REDACTED]" in result.message
    assert active.stop_calls == 1
    assert store.load_runtime("server") is None


def test_unexpected_exception_cleanup_identity_mismatch_has_priority(tmp_path):
    supervisor, store, _, tunnel, active, _ = make_supervisor(tmp_path)
    tunnel.verify.side_effect = RuntimeError("password=do-not-leak")
    tunnel.inspector.matches.return_value = False

    result = supervisor.run(ControlledEvent([]))

    assert result.state is SupervisorState.FAILED
    assert result.error_code == "PROCESS_IDENTITY_MISMATCH"
    assert "do-not-leak" not in result.message
    assert "[REDACTED]" in result.message
    assert store.load_runtime("server") == active.state
    tunnel.inspector.terminate_owned.assert_not_called()
    assert active.stop_calls == 0


def test_unexpected_exception_cleanup_timeout_has_priority(tmp_path):
    supervisor, store, _, tunnel, active, _ = make_supervisor(tmp_path)
    tunnel.verify.side_effect = RuntimeError("unexpected")
    active.stops = False

    result = supervisor.run(ControlledEvent([]))

    assert result.state is SupervisorState.FAILED
    assert result.error_code == "PROCESS_STOP_TIMEOUT"
    assert store.load_runtime("server") == active.state


def test_supervisor_lock_busy_prevents_second_supervisor(tmp_path):
    supervisor, store, _, tunnel, _, _ = make_supervisor(tmp_path)

    with store.supervisor_lock("server"):
        result = supervisor.run(ControlledEvent([]))

    assert result.state is SupervisorState.FAILED
    assert result.error_code == "PROFILE_BUSY"
    tunnel.acquire.assert_not_called()


def test_worker_exit_releases_supervisor_lock(tmp_path):
    supervisor, store, local, _, _, _ = make_supervisor(tmp_path)
    local.inspect.return_value = local_failure()
    store.update_profile(profile(auto_reconnect=False))
    supervisor.run(ControlledEvent([]))

    with store.supervisor_lock("server"):
        pass


def test_stop_event_interrupts_long_backoff_promptly(tmp_path):
    supervisor, _, local, _, _, _ = make_supervisor(tmp_path, backoff=(30.0,))
    local.inspect.return_value = local_failure()
    stop_event = threading.Event()
    worker = threading.Thread(target=supervisor.run, args=(stop_event,))
    worker.start()
    deadline = time.monotonic() + 1
    while supervisor.snapshot().state is not SupervisorState.DEGRADED and time.monotonic() < deadline:
        time.sleep(0.005)

    started = time.monotonic()
    stop_event.set()
    worker.join(0.5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 0.5
    assert supervisor.snapshot().state is SupervisorState.STOPPED


def test_identity_mismatch_on_stop_preserves_evidence_and_never_terminates(tmp_path):
    supervisor, store, _, tunnel, active, _ = make_supervisor(tmp_path)
    tunnel.inspector.matches.return_value = False

    result = supervisor.run(ControlledEvent([True]))

    assert result.error_code == "PROCESS_IDENTITY_MISMATCH"
    assert store.load_runtime("server") == active.state
    tunnel.inspector.terminate_owned.assert_not_called()
    assert active.stop_calls == 0


def test_live_supervisor_lock_blocks_profile_update_until_worker_exits(tmp_path):
    supervisor, store, _, tunnel, _, _ = make_supervisor(tmp_path)
    profiles = ProfileService(store, tunnel)
    stop_event = threading.Event()
    worker = threading.Thread(target=supervisor.run, args=(stop_event,))
    worker.start()
    deadline = time.monotonic() + 1
    while supervisor.snapshot().state is not SupervisorState.READY and time.monotonic() < deadline:
        time.sleep(0.005)

    with pytest.raises(RABError) as caught:
        profiles.update("server", {"local_proxy_port": 7890})
    assert getattr(caught.value, "code", None) == "PROFILE_BUSY"

    stop_event.set()
    worker.join(0.5)
    assert not worker.is_alive()
    assert profiles.update("server", {"local_proxy_port": 7890}).local_proxy_port == 7890


def test_endpoint_terminal_cleanup_identity_mismatch_has_error_priority(tmp_path):
    supervisor, store, _, tunnel, active, _ = make_supervisor(tmp_path)
    store.update_profile(profile(auto_reconnect=False))
    tunnel.verify.return_value = (passed("Listener"), failed("Endpoint", "ENDPOINT_UNREACHABLE"))
    tunnel.inspector.matches.return_value = False

    result = supervisor.run(ControlledEvent([]))

    assert result.state is SupervisorState.FAILED
    assert result.error_code == "PROCESS_IDENTITY_MISMATCH"
    assert "ENDPOINT_UNREACHABLE" in result.message
    assert store.load_runtime("server") == active.state
    tunnel.inspector.terminate_owned.assert_not_called()
    assert active.stop_calls == 0


def test_local_proxy_terminal_cleanup_timeout_has_error_priority(tmp_path):
    supervisor, store, local, _, active, _ = make_supervisor(tmp_path)
    store.update_profile(profile(auto_reconnect=False))
    active.stops = False
    local.inspect.side_effect = [healthy_local(), local_failure()]

    result = supervisor.run(ControlledEvent([False]))

    assert result.state is SupervisorState.FAILED
    assert result.error_code == "PROCESS_STOP_TIMEOUT"
    assert "LOCAL_PROXY_REFUSED" in result.message
    assert store.load_runtime("server") == active.state
