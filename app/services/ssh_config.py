from __future__ import annotations

import shutil

from app.domain.health import CheckResult, CheckStatus
from app.infrastructure.process_runner import ProcessRunner


def locate_ssh() -> str | None:
    return shutil.which("ssh.exe") or shutil.which("ssh")


class SSHConfigService:
    def __init__(self, runner: ProcessRunner, ssh_executable: str, timeout: float = 10.0) -> None:
        self.runner = runner
        self.ssh_executable = ssh_executable
        self.timeout = timeout

    def check(self, target: str) -> CheckResult:
        result = self.runner.run([self.ssh_executable, "-G", target], timeout=self.timeout)
        if result.ok and "hostname " in result.stdout.lower():
            return CheckResult("SSH configuration", CheckStatus.PASS, f"SSH target '{target}' resolves")
        detail = result.stderr.strip() or result.stdout.strip() or "ssh -G failed"
        return CheckResult("SSH configuration", CheckStatus.FAIL, detail[:300], result.error_code or "SSH_CONFIG_INVALID")

