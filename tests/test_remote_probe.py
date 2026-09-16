from app.domain.health import CheckStatus
from app.domain.profile import Profile, RuntimeState
from app.infrastructure.process_runner import ProcessResult
from app.services.remote_probe import RemoteProbeService


class RecordingRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append((argv, timeout))
        return self.result


def profile():
    return Profile(1, "server", "server", local_proxy_port=7897, remote_port=17890)


TUNNEL_ID = "11111111-1111-4111-8111-111111111111"


def runtime_with_remote_identity():
    return RuntimeState(
        1,
        "server",
        4321,
        1234.5,
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        TUNNEL_ID,
        111,
        17890,
        "2026-09-02T00:00:00+00:00",
        remote_user="tester",
        remote_sshd_pid=690949,
        remote_sshd_start_ticks=123456,
        remote_boot_id="11111111-2222-4333-8444-555555555555",
        remote_ssh_connection="192.0.2.10 50000 192.0.2.20 22",
    )


def test_401_remote_probe_is_network_pass_and_uses_argv():
    runner = RecordingRunner(ProcessResult(("ssh.exe",), 0, "401", ""))
    service = RemoteProbeService(runner, "ssh.exe", timeout=10)

    result = service.check_endpoint(profile())

    argv, timeout = runner.calls[0]
    assert result.status is CheckStatus.PASS
    assert result.http_status == 401
    assert isinstance(argv, list)
    assert argv[0] == "ssh.exe"
    assert argv[-2] == "server"
    assert "--proxy" in argv[-1]
    assert argv[-1].startswith("curl --disable ")
    assert "--noproxy ''" in argv[-1]
    assert "127.0.0.1:17890" in argv[-1]


def test_listener_absent_is_distinct_from_ssh_failure():
    absent_runner = RecordingRunner(ProcessResult(("ssh.exe",), 0, "", ""))
    absent = RemoteProbeService(absent_runner, "ssh.exe").check_listener(profile())
    assert absent.error_code == "LISTENER_ABSENT"

    failed_runner = RecordingRunner(
        ProcessResult(("ssh.exe",), 255, "", "administratively prohibited", "PROCESS_EXIT_NONZERO")
    )
    failed = RemoteProbeService(failed_runner, "ssh.exe").check_listener(profile())
    assert failed.error_code == "REMOTE_CHECK_FAILED"
    assert "administratively prohibited" in failed.detail


def test_listener_must_be_bound_to_required_loopback_address():
    runner = RecordingRunner(
        ProcessResult(("ssh.exe",), 0, "LISTEN 0 128 0.0.0.0:17890 0.0.0.0:*\n", "")
    )
    result = RemoteProbeService(runner, "ssh.exe").check_listener(profile())
    assert result.status is CheckStatus.FAIL
    assert result.error_code == "UNSAFE_REMOTE_BINDING"


def test_remote_identity_marker_is_parsed_into_strict_runtime_fields():
    marker = "\n".join(
        [
            "schema_version=1",
            f"tunnel_id={TUNNEL_ID}",
            "remote_port=17890",
            "remote_user=tester",
            "sshd_pid=690949",
            "sshd_start_ticks=123456",
            "boot_id=11111111-2222-4333-8444-555555555555",
            "ssh_connection=192.0.2.10 50000 192.0.2.20 22",
        ]
    )
    runner = RecordingRunner(ProcessResult(("ssh.exe",), 0, marker, ""))

    identity, check = RemoteProbeService(runner, "ssh.exe").read_tunnel_identity(profile(), TUNNEL_ID)

    assert check.passed
    assert identity is not None
    assert identity.sshd_pid == 690949
    assert identity.remote_user == "tester"


def test_marker_creation_uses_unprivileged_listener_check_without_process_visibility():
    from app.services.remote_probe import build_remote_identity_command

    command = build_remote_identity_command(TUNNEL_ID, 17890)

    assert "ss -ltnH 'sport = :17890'" in command
    assert "127.0.0.1:17890" in command
    assert "ss -ltnp" not in command
    assert "listener_pid" not in command
    assert "sudo" not in command
    assert "sshd_pid=$PPID" in command


def test_stale_cleanup_command_verifies_full_identity_before_exact_pid_signal():
    runner = RecordingRunner(ProcessResult(("ssh.exe",), 0, "CLEANED\n", ""))
    state = runtime_with_remote_identity()

    result = RemoteProbeService(runner, "ssh.exe").terminate_verified_stale_session(profile(), state)

    command = runner.calls[0][0][-1]
    assert result.passed
    assert command.index("marker owner mismatch") < command.index('kill -TERM -- "$pid"')
    assert command.index("sshd start time mismatch") < command.index('kill -TERM -- "$pid"')
    assert command.index("remote boot ID mismatch") < command.index('kill -TERM -- "$pid"')
    assert command.index("loopback listener mismatch") < command.index('kill -TERM -- "$pid"')
    assert "ss -ltnp" not in command
    assert "pkill" not in command
    assert "killall" not in command
