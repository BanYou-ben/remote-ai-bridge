from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile, RemoteTunnelIdentity, RuntimeState
from app.infrastructure.process_identity import ProcessIdentity, ProcessInspector
from app.infrastructure.process_runner import ProcessResult
from app.infrastructure.profile_store import ProfileStore
from app.services.tunnel import RemotePortConflictError, TunnelManager, build_tunnel_command


SSH = r"C:\Windows\System32\OpenSSH\ssh.exe"


def profile():
    return Profile(1, "myserver", "myserver", local_proxy_port=7897, remote_port=17890)


TUNNEL_ID = "11111111-1111-4111-8111-111111111111"


def remote_identity(tunnel_id=TUNNEL_ID):
    return RemoteTunnelIdentity(
        tunnel_id=tunnel_id,
        remote_port=17890,
        remote_user="tester",
        sshd_pid=690949,
        sshd_start_ticks=123456,
        boot_id="11111111-2222-4333-8444-555555555555",
        ssh_connection="192.0.2.10 50000 192.0.2.20 22",
    )


def runtime(executable=SSH, with_remote_identity=False):
    remote = remote_identity() if with_remote_identity else None
    return RuntimeState(
        1,
        "myserver",
        4321,
        1234.5,
        executable,
        TUNNEL_ID,
        111,
        17890,
        "2026-09-02T00:00:00+00:00",
        remote_user=remote.remote_user if remote else None,
        remote_sshd_pid=remote.sshd_pid if remote else None,
        remote_sshd_start_ticks=remote.sshd_start_ticks if remote else None,
        remote_boot_id=remote.boot_id if remote else None,
        remote_ssh_connection=remote.ssh_connection if remote else None,
    )


def manager(tmp_path, inspector=None, runner=None, remote=None):
    return TunnelManager(
        runner or MagicMock(),
        inspector or MagicMock(spec=ProcessInspector),
        ProfileStore(tmp_path),
        remote or MagicMock(),
        SSH,
        startup_timeout=0.001,
        remote_identity_timeout=0.01,
        stop_timeout=0.01,
    )


def test_tunnel_command_matches_required_safe_shape():
    command = build_tunnel_command(SSH, profile(), TUNNEL_ID)
    assert command[0] == SSH
    assert command[-2] == "myserver"
    assert TUNNEL_ID in command[-1]
    assert "remote_port=%s" in command[-1]
    assert "17890" in command[-1]
    assert "-N" not in command
    assert command[command.index("-R") + 1] == "127.0.0.1:17890:127.0.0.1:7897"
    assert "ExitOnForwardFailure=yes" in command
    assert "ServerAliveInterval=30" in command
    assert "ServerAliveCountMax=3" in command
    assert "StrictHostKeyChecking=yes" in command
    assert all(isinstance(part, str) for part in command)


def test_unknown_remote_port_owner_is_never_terminated(tmp_path):
    inspector = MagicMock(spec=ProcessInspector)
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult("Remote listener", CheckStatus.PASS, "occupied")
    remote.find_free_port.return_value = 17891
    service = manager(tmp_path, inspector=inspector, remote=remote)

    with pytest.raises(RemotePortConflictError) as raised:
        service.acquire(profile())

    assert raised.value.suggested_port == 17891
    inspector.terminate_owned.assert_not_called()
    service.runner.start_managed.assert_not_called()


def test_owned_recorded_tunnel_is_reused(tmp_path):
    store = ProfileStore(tmp_path)
    state = runtime()
    store.save_runtime(state)
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = True
    remote = MagicMock()
    service = TunnelManager(MagicMock(), inspector, store, remote, SSH)

    active = service.acquire(profile())

    assert active.state == state
    assert active.process is None
    remote.check_listener.assert_not_called()


def test_disconnect_identity_mismatch_never_terminates_pid(tmp_path):
    store = ProfileStore(tmp_path)
    store.save_runtime(runtime())
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = False
    service = TunnelManager(MagicMock(), inspector, store, MagicMock(), SSH)

    result = service.disconnect("myserver")

    assert result.error_code == "PROCESS_IDENTITY_MISMATCH"
    inspector.terminate_owned.assert_not_called()
    assert store.load_runtime("myserver") is None


def test_process_ownership_matches_pid_creation_time_and_executable():
    inspector = ProcessInspector()
    state = runtime()
    inspector.get_identity = MagicMock(return_value=ProcessIdentity(4321, 1234.5, SSH))
    assert inspector.matches(state)

    inspector.get_identity = MagicMock(return_value=ProcessIdentity(4321, 1235.0, SSH))
    assert not inspector.matches(state)

    inspector.get_identity = MagicMock(return_value=ProcessIdentity(4321, 1234.5, r"C:\Windows\notepad.exe"))
    assert not inspector.matches(state)


class FakeManaged:
    pid = 4321

    def poll(self):
        return None

    def stop(self, timeout):
        return ProcessResult((SSH,), 0, "", "")

    def collect_after_exit(self):
        return ProcessResult((SSH,), 255, "", "remote port forwarding failed: administratively prohibited")


def test_new_tunnel_records_verified_process_identity(tmp_path):
    inspector = MagicMock(spec=ProcessInspector)
    resolved = str(Path(SSH).resolve())
    inspector.get_identity.return_value = ProcessIdentity(4321, 1234.5, resolved)
    runner = MagicMock()
    runner.start_managed.return_value = FakeManaged()
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult("Remote listener", CheckStatus.FAIL, "absent", "LISTENER_ABSENT")
    remote.read_tunnel_identity.side_effect = lambda value, tunnel_id: (
        remote_identity(tunnel_id),
        CheckResult("Remote tunnel identity", CheckStatus.PASS, "ok"),
    )
    remote.verify_stale_session_ownership.return_value = CheckResult(
        "Remote stale session ownership", CheckStatus.PASS, "verified"
    )
    service = manager(tmp_path, inspector=inspector, runner=runner, remote=remote)

    active = service.acquire(profile())

    saved = service.store.load_runtime("myserver")
    assert saved is not None
    assert saved.pid == 4321
    assert saved.tunnel_id
    assert saved.remote_sshd_pid == 690949
    assert active.process is runner.start_managed.return_value


def test_confirmed_owned_stale_session_is_cleaned_and_original_port_reused(tmp_path):
    store = ProfileStore(tmp_path)
    stale = runtime(with_remote_identity=True)
    store.save_runtime(stale)
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = False
    runner = MagicMock()
    runner.start_managed.return_value = FakeManaged()
    resolved = str(Path(SSH).resolve())
    inspector.get_identity.return_value = ProcessIdentity(4321, 1234.5, resolved)
    remote = MagicMock()
    remote.check_listener.side_effect = [
        CheckResult("Remote listener", CheckStatus.PASS, "occupied"),
        CheckResult("Remote listener", CheckStatus.FAIL, "absent", "LISTENER_ABSENT"),
    ]
    remote.verify_stale_session_ownership.return_value = CheckResult(
        "Remote stale session ownership", CheckStatus.PASS, "verified"
    )
    remote.terminate_verified_stale_session.return_value = CheckResult(
        "Remote stale session cleanup", CheckStatus.PASS, "cleaned"
    )
    remote.read_tunnel_identity.side_effect = lambda value, tunnel_id: (
        remote_identity(tunnel_id),
        CheckResult("Remote tunnel identity", CheckStatus.PASS, "ok"),
    )
    service = TunnelManager(
        runner,
        inspector,
        store,
        remote,
        SSH,
        startup_timeout=0.001,
        remote_identity_timeout=0.01,
        stop_timeout=0.01,
    )

    active = service.acquire(profile())

    remote.verify_stale_session_ownership.assert_any_call(profile(), stale)
    remote.terminate_verified_stale_session.assert_called_once_with(profile(), stale)
    started_argv = runner.start_managed.call_args.args[0]
    assert started_argv[started_argv.index("-R") + 1] == "127.0.0.1:17890:127.0.0.1:7897"
    assert service.store.load_runtime("myserver").remote_port == 17890
    assert active.state.remote_port == 17890


def test_remote_identity_mismatch_never_cleans_or_starts(tmp_path):
    store = ProfileStore(tmp_path)
    stale = runtime(with_remote_identity=True)
    store.save_runtime(stale)
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = False
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult("Remote listener", CheckStatus.PASS, "occupied")
    remote.verify_stale_session_ownership.return_value = CheckResult(
        "Remote stale session ownership", CheckStatus.FAIL, "mismatch", "REMOTE_IDENTITY_MISMATCH"
    )
    service = TunnelManager(MagicMock(), inspector, store, remote, SSH)

    with pytest.raises(RemotePortConflictError) as raised:
        service.acquire(profile())

    assert raised.value.suggested_port is None
    remote.terminate_verified_stale_session.assert_not_called()
    service.runner.start_managed.assert_not_called()
    assert store.load_runtime("myserver") == stale


def test_missing_remote_identity_never_cleans_or_starts(tmp_path):
    store = ProfileStore(tmp_path)
    stale = runtime(with_remote_identity=False)
    store.save_runtime(stale)
    inspector = MagicMock(spec=ProcessInspector)
    inspector.matches.return_value = False
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult("Remote listener", CheckStatus.PASS, "occupied")
    remote.verify_stale_session_ownership.return_value = CheckResult(
        "Remote stale session ownership", CheckStatus.FAIL, "unavailable", "REMOTE_IDENTITY_UNAVAILABLE"
    )
    service = TunnelManager(MagicMock(), inspector, store, remote, SSH)

    with pytest.raises(RemotePortConflictError) as raised:
        service.acquire(profile())

    assert raised.value.suggested_port is None
    remote.terminate_verified_stale_session.assert_not_called()
    service.runner.start_managed.assert_not_called()
    assert store.load_runtime("myserver") == stale


def test_wildcard_bound_unknown_listener_is_treated_as_conflict(tmp_path):
    inspector = MagicMock(spec=ProcessInspector)
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult(
        "Remote listener", CheckStatus.FAIL, "unsafe wildcard", "UNSAFE_REMOTE_BINDING"
    )
    remote.find_free_port.return_value = 17891
    service = manager(tmp_path, inspector=inspector, remote=remote)
    with pytest.raises(RemotePortConflictError):
        service.acquire(profile())
    inspector.terminate_owned.assert_not_called()


class ExitedManaged(FakeManaged):
    def poll(self):
        return 255


def test_forwarding_disabled_is_reported_specifically(tmp_path):
    inspector = MagicMock(spec=ProcessInspector)
    remote = MagicMock()
    remote.check_listener.return_value = CheckResult(
        "Remote listener", CheckStatus.FAIL, "absent", "LISTENER_ABSENT"
    )
    service = manager(tmp_path, inspector=inspector, remote=remote)
    active = MagicMock()
    active.poll.return_value = 255
    active.exit_result.return_value = ProcessResult(
        (SSH,), 255, "", "remote port forwarding failed: administratively prohibited"
    )

    listener, endpoint = service.verify(profile(), active, timeout=0.001)

    assert listener.error_code == "SSH_FORWARDING_DENIED"
    assert endpoint.status is CheckStatus.SKIP
