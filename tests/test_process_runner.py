import subprocess
import sys
from unittest.mock import patch

from app.cli import redact
from app.infrastructure.process_runner import ProcessRunner


def test_run_uses_argument_array_no_shell_and_captures_output():
    completed = subprocess.CompletedProcess(["ssh.exe", "-G", "host"], 0, "hostname host\n", "warning\n")
    with patch("app.infrastructure.process_runner.subprocess.run", return_value=completed) as run:
        result = ProcessRunner().run(["ssh.exe", "-G", "host"], timeout=4)

    assert result.ok
    assert result.stdout == "hostname host\n"
    assert result.stderr == "warning\n"
    assert run.call_args.kwargs["shell"] is False
    assert run.call_args.kwargs["timeout"] == 4
    assert run.call_args.args[0] == ("ssh.exe", "-G", "host")


def test_timeout_is_normalized():
    with patch(
        "app.infrastructure.process_runner.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["ssh.exe"], 1, output=b"partial", stderr=b"late"),
    ):
        result = ProcessRunner().run(["ssh.exe"], timeout=1)
    assert result.timed_out
    assert result.error_code == "PROCESS_TIMEOUT"
    assert result.stdout == "partial"
    assert result.stderr == "late"


def test_missing_executable_is_normalized():
    with patch("app.infrastructure.process_runner.subprocess.run", side_effect=FileNotFoundError("missing")):
        result = ProcessRunner().run(["missing.exe"], timeout=1)
    assert result.error_code == "EXECUTABLE_NOT_FOUND"
    assert result.exit_code is None


def test_console_redaction_covers_common_secret_shapes():
    value = (
        "Authorization: Bearer abc.def api_key=secret http://user:password@proxy\n"
        "Cookie: session=hidden\n-----BEGIN PRIVATE KEY-----\nkey material\n-----END PRIVATE KEY-----"
    )
    output = redact(value)
    assert "abc.def" not in output
    assert "secret" not in output
    assert "password" not in output
    assert "session=hidden" not in output
    assert "key material" not in output
    assert output.count("[REDACTED]") >= 5


def test_managed_process_captures_output_without_pipe_deadlock():
    runner = ProcessRunner()
    process = runner.start_managed(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"]
    )
    result = process.collect_after_exit(timeout=5)
    assert result.exit_code == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
