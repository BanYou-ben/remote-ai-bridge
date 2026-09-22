from types import SimpleNamespace
from unittest.mock import MagicMock

from app import cli
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile, RuntimeState
from app.domain.supervisor import SupervisorSnapshot, SupervisorState, utc_now
from app.infrastructure.profile_store import ProfileStore
from app.services.profile_service import ProfileService
from app.services.runtime_manager import RuntimeManager


def passed(name: str) -> CheckResult:
    return CheckResult(name, CheckStatus.PASS, "ok")


def profile(auto_reconnect: bool = True) -> Profile:
    return Profile(1, "myserver", "myserver", local_proxy_port=7897, remote_port=17890, auto_reconnect=auto_reconnect)


def snapshot(state, *, code=None, message="state", runtime=True):
    return SupervisorSnapshot(
        "myserver",
        state,
        utc_now(),
        error_code=code,
        message=message,
        runtime_present=runtime,
    )


class RuntimeStub:
    def __init__(self, snapshots):
        self.snapshots = iter(snapshots)
        self.start_calls = []
        self.wait_calls = []
        self.stop_calls = []

    def start(self, name):
        self.start_calls.append(name)
        return next(self.snapshots)

    def wait(self, name, timeout):
        self.wait_calls.append((name, timeout))
        value = next(self.snapshots)
        if isinstance(value, BaseException):
            raise value
        return value

    def stop(self, name):
        self.stop_calls.append(name)
        return SimpleNamespace(
            worker_exited=True,
            snapshot=snapshot(SupervisorState.STOPPED, message="supervision stopped", runtime=False),
        )


def test_health_failure_keeps_live_owned_tunnel_and_reuses_it(capsys):
    runtime = RuntimeStub(
        [
            snapshot(SupervisorState.STARTING),
            snapshot(SupervisorState.READY),
            snapshot(SupervisorState.DEGRADED, code="ENDPOINT_UNREACHABLE"),
            snapshot(SupervisorState.READY),
            KeyboardInterrupt(),
        ]
    )
    ssh_config = MagicMock()
    ssh_config.check.return_value = passed("SSH configuration")

    result = cli.command_connect(profile(), ssh_config, runtime)

    assert result == 0
    assert runtime.start_calls == ["myserver"]
    assert runtime.stop_calls == ["myserver"]
    output = capsys.readouterr().out
    assert "DEGRADED [ENDPOINT_UNREACHABLE]" in output
    assert output.count("READY") == 2


def test_runtime_is_retained_for_remote_ownership_check_after_ssh_exit(capsys):
    runtime = RuntimeStub(
        [
            snapshot(SupervisorState.STARTING),
            snapshot(
                SupervisorState.DEGRADED,
                code="TUNNEL_EXITED",
                message="runtime ownership evidence is retained",
            ),
            KeyboardInterrupt(),
        ]
    )
    ssh_config = MagicMock()
    ssh_config.check.return_value = passed("SSH configuration")

    result = cli.command_connect(profile(), ssh_config, runtime)

    assert result == 0
    assert "runtime ownership evidence is retained" in capsys.readouterr().out
    assert runtime.stop_calls == ["myserver"]


def test_endpoint_path_failure_is_not_reported_as_local_proxy_failure(capsys):
    runtime = RuntimeStub(
        [
            snapshot(
                SupervisorState.FAILED,
                code="ENDPOINT_UNREACHABLE",
                message="network/endpoint path unhealthy",
            )
        ]
    )
    ssh_config = MagicMock()
    ssh_config.check.return_value = passed("SSH configuration")

    result = cli.command_connect(profile(auto_reconnect=False), ssh_config, runtime)

    output = capsys.readouterr().out
    assert result == 1
    assert "network/endpoint path unhealthy" in output
    assert "local proxy unhealthy" not in output
    assert runtime.stop_calls == []


def test_connect_returns_before_start_when_ssh_config_preflight_fails():
    runtime = MagicMock()
    ssh_config = MagicMock()
    ssh_config.check.return_value = CheckResult("SSH", CheckStatus.FAIL, "bad", "SSH_CONFIG_FAILED")

    assert cli.command_connect(profile(), ssh_config, runtime) == 1
    runtime.start.assert_not_called()


def test_remote_port_conflict_is_only_displayed_and_never_changes_profile(capsys):
    runtime = RuntimeStub(
        [
            SupervisorSnapshot(
                "myserver",
                SupervisorState.FAILED,
                utc_now(),
                error_code="REMOTE_PORT_CONFLICT",
                message="occupied",
                suggested_port=17891,
            )
        ]
    )
    ssh_config = MagicMock()
    ssh_config.check.return_value = passed("SSH configuration")
    original = profile()

    assert cli.command_connect(original, ssh_config, runtime) == 1
    assert original.remote_port == 17890
    assert "suggested remote port 17891" in capsys.readouterr().out


def test_status_delegates_to_runtime_manager(capsys):
    runtime = MagicMock()
    runtime.status.return_value = SupervisorSnapshot(
        "myserver",
        SupervisorState.UNSUPERVISED,
        utc_now(),
        message="owned process exists",
        supervised=False,
        runtime_present=True,
        process_alive=True,
    )

    assert cli.command_status(profile(), runtime) == 0
    runtime.status.assert_called_once_with("myserver")
    assert "UNSUPERVISED" in capsys.readouterr().out


def test_disconnect_delegates_to_runtime_manager():
    runtime = MagicMock()
    runtime.stop.return_value = SimpleNamespace(
        snapshot=snapshot(SupervisorState.STOPPED, message="stopped", runtime=False)
    )

    cli.command_disconnect(profile(), runtime)

    runtime.stop.assert_called_once_with("myserver")


def test_disconnect_stopped_snapshot_returns_success(capsys):
    runtime = MagicMock()
    runtime.stop.return_value = SimpleNamespace(
        snapshot=snapshot(SupervisorState.STOPPED, message="stopped", runtime=False)
    )

    assert cli.command_disconnect(profile(), runtime) == 0
    assert "STOPPED" in capsys.readouterr().out


def test_disconnect_identity_mismatch_returns_failure(capsys):
    runtime = MagicMock()
    runtime.stop.return_value = SimpleNamespace(
        snapshot=snapshot(
            SupervisorState.FAILED,
            code="PROCESS_IDENTITY_MISMATCH",
            message="identity mismatch",
        )
    )

    assert cli.command_disconnect(profile(), runtime) == 1
    assert "PROCESS_IDENTITY_MISMATCH" in capsys.readouterr().out


def test_disconnect_profile_busy_returns_failure_without_cli_process_action(capsys):
    runtime = MagicMock()
    runtime.stop.return_value = SimpleNamespace(
        snapshot=snapshot(SupervisorState.FAILED, code="PROFILE_BUSY", message="busy")
    )

    assert cli.command_disconnect(profile(), runtime) == 1
    runtime.stop.assert_called_once_with("myserver")
    assert "PROFILE_BUSY" in capsys.readouterr().out


def test_cli_disconnect_preserves_mismatched_runtime_evidence(tmp_path):
    selected = profile()
    store = ProfileStore(tmp_path)
    store.save_profile(selected)
    state = RuntimeState(
        schema_version=1,
        profile_name="myserver",
        pid=123,
        process_creation_time=1.0,
        executable_path="C:/Windows/System32/OpenSSH/ssh.exe",
        tunnel_id="tunnel-1",
        supervisor_pid=456,
        remote_port=17890,
        started_at="2026-01-01T00:00:00+00:00",
    )
    store.save_runtime(state)
    tunnel = MagicMock()
    tunnel.inspector.matches.return_value = False
    profiles = ProfileService(store, tunnel)
    runtime = RuntimeManager(profiles, store, MagicMock(), tunnel)

    assert cli.command_disconnect(selected, runtime) == 1
    assert store.load_runtime("myserver") == state
    tunnel.disconnect.assert_not_called()
    tunnel.inspector.terminate_owned.assert_not_called()
