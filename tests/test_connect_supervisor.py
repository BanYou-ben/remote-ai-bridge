from unittest.mock import MagicMock

from app import cli
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile
from app.services.local_proxy import LocalProxyReport


def passed(name: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASS, "ok")


def failed(name: str, code: str = "FAILED") -> CheckResult:
    return CheckResult(name, CheckStatus.FAIL, "failed", code)


def healthy_local() -> LocalProxyReport:
    return LocalProxyReport(passed("TCP reachable"), passed("HTTP proxy handshake"), passed("AI endpoint probe"))


def profile(auto_reconnect: bool = True) -> Profile:
    return Profile(1, "myserver", "myserver", local_proxy_port=7897, remote_port=17890, auto_reconnect=auto_reconnect)


class ActiveTunnelStub:
    def __init__(self, events: list[str], poll_values=None) -> None:
        self.events = events
        self.poll_values = iter(poll_values or [])

    def poll(self):
        try:
            return next(self.poll_values)
        except StopIteration:
            return None

    def stop(self, timeout):
        self.events.append("stop")
        return True


class StoreStub:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.saved_profiles = []

    def clear_runtime(self, name):
        self.events.append("clear")

    def load_runtime(self, name):
        return None

    def save_profile(self, profile, overwrite=False):
        self.saved_profiles.append((profile, overwrite))


def test_health_failure_keeps_live_owned_tunnel_and_reuses_it(monkeypatch):
    events: list[str] = []
    active = ActiveTunnelStub(events)
    store = StoreStub(events)
    local = MagicMock()
    local.inspect.return_value = healthy_local()
    ssh_config = MagicMock()
    ssh_config.check.return_value = passed("SSH configuration")
    tunnel = MagicMock()
    tunnel.stop_timeout = 5
    tunnel.inspector = MagicMock()
    tunnel.acquire.side_effect = lambda value: events.append("acquire") or active
    verification_results = iter([
        (passed("Remote listener"), passed("Remote endpoint probe")),
        (passed("Remote listener"), failed("Remote endpoint probe")),
        (passed("Remote listener"), passed("Remote endpoint probe")),
    ])
    verify_count = 0

    def recorded_verify(*args):
        nonlocal verify_count
        verify_count += 1
        events.append(f"verify-{verify_count}")
        return next(verification_results)

    tunnel.verify.side_effect = recorded_verify

    sleep_calls = 0

    def controlled_sleep(seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "HEALTH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cli.time, "sleep", controlled_sleep)

    result = cli.command_connect(profile(), store, local, ssh_config, tunnel)

    assert result == 0
    assert tunnel.acquire.call_count == 1
    assert tunnel.verify.call_count == 3
    assert events == ["acquire", "verify-1", "verify-2", "verify-3", "stop", "clear"]
    # The only stop/clear pair is Ctrl+C cleanup after the third verify recovered.
    assert "stop" not in events[events.index("verify-2") + 1 : events.index("verify-3")]
    assert "clear" not in events[events.index("verify-2") + 1 : events.index("verify-3")]
    assert store.saved_profiles == []


def test_runtime_is_retained_for_remote_ownership_check_after_ssh_exit(monkeypatch):
    events: list[str] = []
    active = ActiveTunnelStub(events, poll_values=[None, 255])
    store = StoreStub(events)
    local = MagicMock()
    local.inspect.return_value = healthy_local()
    ssh_config = MagicMock()
    ssh_config.check.return_value = passed("SSH configuration")
    tunnel = MagicMock()
    tunnel.stop_timeout = 5
    tunnel.inspector = MagicMock()

    def acquire(value):
        events.append("acquire")
        if events.count("acquire") == 2:
            raise KeyboardInterrupt
        return active

    tunnel.acquire.side_effect = acquire
    verification_results = iter([
        (passed("Remote listener"), passed("Remote endpoint probe")),
        (passed("Remote listener"), failed("Remote endpoint probe")),
    ])
    verify_count = 0

    def verify(*args):
        nonlocal verify_count
        verify_count += 1
        events.append(f"verify-{verify_count}")
        return next(verification_results)

    tunnel.verify.side_effect = verify
    monkeypatch.setattr(cli, "HEALTH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)

    result = cli.command_connect(profile(), store, local, ssh_config, tunnel)

    assert result == 0
    assert events == ["acquire", "verify-1", "verify-2", "acquire"]
    assert "stop" not in events


def test_endpoint_path_failure_is_not_reported_as_local_proxy_failure(capsys):
    local = MagicMock()
    local.inspect.return_value = LocalProxyReport(
        passed("TCP reachable"),
        passed("HTTP proxy handshake"),
        failed("AI endpoint probe", "ENDPOINT_UNREACHABLE"),
    )
    ssh_config = MagicMock()
    ssh_config.check.return_value = passed("SSH configuration")
    tunnel = MagicMock()
    store = StoreStub([])

    result = cli.command_connect(profile(auto_reconnect=False), store, local, ssh_config, tunnel)

    output = capsys.readouterr().out
    assert result == 1
    assert "network/endpoint path unhealthy" in output
    assert "local proxy unhealthy" not in output
    tunnel.acquire.assert_not_called()
