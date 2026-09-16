from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import tempfile
import time
from typing import IO, Sequence


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    error_code: str | None = None
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.error_code is None and self.exit_code == 0


class ManagedProcess:
    def __init__(
        self,
        process: subprocess.Popen[str],
        argv: Sequence[str],
        stdout_file: IO[str],
        stderr_file: IO[str],
    ) -> None:
        self._process = process
        self.argv = tuple(argv)
        self._stdout_file = stdout_file
        self._stderr_file = stderr_file
        self._collected: ProcessResult | None = None

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def stop(self, timeout: float) -> ProcessResult:
        if self.poll() is None:
            self._process.terminate()
        try:
            self._process.wait(timeout=timeout)
            return self._collect()
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=max(1.0, timeout))
            result = self._collect()
            return ProcessResult(
                self.argv,
                self._process.returncode,
                result.stdout,
                result.stderr,
                error_code="PROCESS_FORCE_KILLED",
                timed_out=True,
            )

    def collect_after_exit(self, timeout: float = 1.0) -> ProcessResult:
        try:
            self._process.wait(timeout=timeout)
            return self._collect()
        except subprocess.TimeoutExpired:
            return ProcessResult(self.argv, None, "", "", "PROCESS_STILL_RUNNING", True)

    def _collect(self) -> ProcessResult:
        if self._collected is not None:
            return self._collected
        self._stdout_file.flush()
        self._stderr_file.flush()
        self._stdout_file.seek(0)
        self._stderr_file.seek(0)
        stdout = self._stdout_file.read()
        stderr = self._stderr_file.read()
        self._stdout_file.close()
        self._stderr_file.close()
        self._collected = ProcessResult(self.argv, self._process.returncode, stdout, stderr)
        return self._collected


class ProcessRunner:
    def run(self, argv: Sequence[str], timeout: float) -> ProcessResult:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        safe_argv = tuple(str(value) for value in argv)
        try:
            completed = subprocess.run(
                safe_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return ProcessResult(
                safe_argv,
                None,
                _to_text(exc.stdout),
                _to_text(exc.stderr),
                error_code="PROCESS_TIMEOUT",
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return ProcessResult(safe_argv, None, "", str(exc), "EXECUTABLE_NOT_FOUND")
        except OSError as exc:
            return ProcessResult(safe_argv, None, "", str(exc), "PROCESS_START_FAILED")
        error_code = None if completed.returncode == 0 else "PROCESS_EXIT_NONZERO"
        return ProcessResult(
            safe_argv,
            completed.returncode,
            completed.stdout or "",
            completed.stderr or "",
            error_code,
        )

    def start_managed(self, argv: Sequence[str]) -> ManagedProcess | ProcessResult:
        safe_argv = tuple(str(value) for value in argv)
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        stdout_file = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
        stderr_file = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
        try:
            process = subprocess.Popen(
                safe_argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=creation_flags,
            )
        except FileNotFoundError as exc:
            stdout_file.close()
            stderr_file.close()
            return ProcessResult(safe_argv, None, "", str(exc), "EXECUTABLE_NOT_FOUND")
        except OSError as exc:
            stdout_file.close()
            stderr_file.close()
            return ProcessResult(safe_argv, None, "", str(exc), "PROCESS_START_FAILED")
        return ManagedProcess(process, safe_argv, stdout_file, stderr_file)


def wait_for_start(process: ManagedProcess, timeout: float, poll_interval: float = 0.05) -> ProcessResult | None:
    """Return an early-exit result, or None once the bounded startup window passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return process.collect_after_exit()
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
    return None


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
