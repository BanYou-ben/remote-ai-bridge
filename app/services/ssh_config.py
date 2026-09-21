from __future__ import annotations

import shutil
from pathlib import Path

from app.domain.profile import Profile
from app.domain.health import CheckResult, CheckStatus
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.profile_store import default_state_root
from app.infrastructure.ssh_key_store import managed_ssh_options


def locate_ssh() -> str | None:
    return shutil.which("ssh.exe") or shutil.which("ssh")


class SSHConfigService:
    def __init__(
        self,
        runner: ProcessRunner,
        ssh_executable: str,
        timeout: float = 10.0,
        state_root: Path | None = None,
    ) -> None:
        self.runner = runner
        self.ssh_executable = ssh_executable
        self.timeout = timeout
        self.state_root = state_root or default_state_root()

    def check(self, target: str | Profile) -> CheckResult:
        profile = target if isinstance(target, Profile) else None
        display_target = profile.ssh_target if profile is not None else target
        argv = [self.ssh_executable, "-G"]
        if profile is not None:
            argv.extend(managed_ssh_options(profile, self.state_root))
        argv.append(display_target)
        result = self.runner.run(argv, timeout=self.timeout)
        if result.ok and "hostname " in result.stdout.lower():
            return CheckResult("SSH configuration", CheckStatus.PASS, f"SSH target '{display_target}' resolves")
        detail = result.stderr.strip() or result.stdout.strip() or "ssh -G failed"
        return CheckResult("SSH configuration", CheckStatus.FAIL, detail[:300], result.error_code or "SSH_CONFIG_INVALID")
