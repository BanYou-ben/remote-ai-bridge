from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import time
import uuid

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile, RemoteTunnelIdentity, RuntimeState
from app.infrastructure.process_identity import ProcessInspector
from app.infrastructure.process_runner import ManagedProcess, ProcessResult, ProcessRunner, wait_for_start
from app.infrastructure.profile_store import ProfileStore
from app.services.remote_probe import RemoteProbeService, build_remote_identity_command


class TunnelError(RuntimeError):
    def __init__(self, message: str, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class RemotePortConflictError(TunnelError):
    def __init__(self, port: int, suggested_port: int | None) -> None:
        message = f"remote port {port} is owned by an unknown process; it will not be terminated"
        if suggested_port is not None:
            message += f"; suggested free port: {suggested_port}"
        super().__init__(message, "REMOTE_PORT_CONFLICT")
        self.port = port
        self.suggested_port = suggested_port


def build_tunnel_command(ssh_executable: str, profile: Profile, tunnel_id: str) -> list[str]:
    profile.validate()
    return [
        ssh_executable,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-R",
        f"{profile.remote_bind_host}:{profile.remote_port}:{profile.local_proxy_host}:{profile.local_proxy_port}",
        profile.ssh_target,
        build_remote_identity_command(tunnel_id, profile.remote_port),
    ]


@dataclass
class ActiveTunnel:
    state: RuntimeState
    inspector: ProcessInspector
    process: ManagedProcess | None = None

    def poll(self) -> int | None:
        if self.process is not None:
            return self.process.poll()
        return None if self.inspector.matches(self.state) else 1

    def stop(self, timeout: float) -> bool:
        if not self.inspector.matches(self.state):
            return self.poll() is not None
        if self.process is not None:
            self.process.stop(timeout)
            return self.process.poll() is not None
        return self.inspector.terminate_owned(self.state, timeout)

    def exit_result(self) -> ProcessResult | None:
        if self.process is None or self.process.poll() is None:
            return None
        return self.process.collect_after_exit()


class TunnelManager:
    def __init__(
        self,
        runner: ProcessRunner,
        inspector: ProcessInspector,
        store: ProfileStore,
        remote_probe: RemoteProbeService,
        ssh_executable: str,
        startup_timeout: float = 1.0,
        remote_identity_timeout: float = 8.0,
        stop_timeout: float = 5.0,
    ) -> None:
        self.runner = runner
        self.inspector = inspector
        self.store = store
        self.remote_probe = remote_probe
        self.ssh_executable = ssh_executable
        self.startup_timeout = startup_timeout
        self.remote_identity_timeout = remote_identity_timeout
        self.stop_timeout = stop_timeout

    def acquire(self, profile: Profile) -> ActiveTunnel:
        existing = self.store.load_runtime(profile.name)
        if existing is not None and existing.remote_port == profile.remote_port and self.inspector.matches(existing):
            return ActiveTunnel(existing, self.inspector)

        listener = self.remote_probe.check_listener(profile)
        if listener.passed or listener.error_code == "UNSAFE_REMOTE_BINDING":
            if existing is not None and existing.remote_port == profile.remote_port:
                ownership = self.remote_probe.verify_stale_session_ownership(profile, existing)
                if ownership.passed:
                    cleanup = self.remote_probe.terminate_verified_stale_session(profile, existing)
                    if cleanup.passed:
                        released = self.remote_probe.check_listener(profile)
                        if released.error_code == "LISTENER_ABSENT":
                            self.store.clear_runtime(profile.name)
                            listener = released
                        else:
                            raise RemotePortConflictError(profile.remote_port, None)
                    else:
                        raise RemotePortConflictError(profile.remote_port, None)
                else:
                    raise RemotePortConflictError(profile.remote_port, None)
            else:
                suggestion = self.remote_probe.find_free_port(profile)
                raise RemotePortConflictError(profile.remote_port, suggestion)
        if listener.error_code != "LISTENER_ABSENT":
            raise TunnelError(listener.detail, listener.error_code or "REMOTE_CHECK_FAILED")

        self.store.clear_runtime(profile.name)
        tunnel_id = str(uuid.uuid4())
        argv = build_tunnel_command(self.ssh_executable, profile, tunnel_id)
        started = self.runner.start_managed(argv)
        if isinstance(started, ProcessResult):
            raise TunnelError(started.stderr or "could not start SSH", started.error_code or "TUNNEL_START_FAILED")
        early_exit = wait_for_start(started, self.startup_timeout)
        if early_exit is not None:
            detail = early_exit.stderr.strip() or early_exit.stdout.strip() or "SSH exited during startup"
            raise TunnelError(detail[:500], "TUNNEL_START_FAILED")

        identity = self._wait_for_identity(started.pid, self.startup_timeout)
        if identity is None:
            started.stop(self.stop_timeout)
            raise TunnelError("could not establish SSH process identity", "PROCESS_IDENTITY_UNAVAILABLE")
        expected_path = str(Path(self.ssh_executable).resolve())
        if os.path.normcase(os.path.abspath(identity.executable_path)) != os.path.normcase(os.path.abspath(expected_path)):
            started.stop(self.stop_timeout)
            raise TunnelError("started process executable identity did not match ssh.exe", "PROCESS_IDENTITY_MISMATCH")

        remote_identity, remote_identity_check = self._wait_for_remote_identity(
            profile, tunnel_id, self.remote_identity_timeout
        )
        if remote_identity is None:
            started.stop(self.stop_timeout)
            raise TunnelError(remote_identity_check.detail, remote_identity_check.error_code or "REMOTE_IDENTITY_UNAVAILABLE")

        state = RuntimeState(
            schema_version=1,
            profile_name=profile.name,
            pid=identity.pid,
            process_creation_time=identity.creation_time,
            executable_path=identity.executable_path,
            tunnel_id=tunnel_id,
            supervisor_pid=os.getpid(),
            remote_port=profile.remote_port,
            started_at=datetime.now(timezone.utc).isoformat(),
            remote_user=remote_identity.remote_user,
            remote_sshd_pid=remote_identity.sshd_pid,
            remote_sshd_start_ticks=remote_identity.sshd_start_ticks,
            remote_boot_id=remote_identity.boot_id,
            remote_ssh_connection=remote_identity.ssh_connection,
        )
        ownership = self.remote_probe.verify_stale_session_ownership(profile, state)
        if not ownership.passed:
            started.stop(self.stop_timeout)
            raise TunnelError(ownership.detail, ownership.error_code or "REMOTE_IDENTITY_MISMATCH")
        self.store.save_runtime(state)
        return ActiveTunnel(state, self.inspector, started)

    def verify(self, profile: Profile, tunnel: ActiveTunnel, timeout: float = 15.0) -> tuple[CheckResult, CheckResult]:
        deadline = time.monotonic() + timeout
        listener = self.remote_probe.check_listener(profile)
        while not listener.passed and listener.error_code == "LISTENER_ABSENT" and time.monotonic() < deadline:
            if tunnel.poll() is not None:
                exited = tunnel.exit_result()
                detail = "SSH tunnel exited"
                error_code = "TUNNEL_EXITED"
                if exited is not None:
                    detail = exited.stderr.strip() or exited.stdout.strip() or detail
                    lowered = detail.lower()
                    if any(
                        marker in lowered
                        for marker in ("administratively prohibited", "remote port forwarding failed", "forwarding disabled")
                    ):
                        error_code = "SSH_FORWARDING_DENIED"
                return (
                    CheckResult("Remote listener", CheckStatus.FAIL, detail[:500], error_code),
                    CheckResult("Remote endpoint probe", CheckStatus.SKIP, "listener unavailable"),
                )
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            listener = self.remote_probe.check_listener(profile)
        if not listener.passed:
            return listener, CheckResult("Remote endpoint probe", CheckStatus.SKIP, "listener unavailable")
        endpoint = self.remote_probe.check_endpoint(profile)
        if endpoint.passed:
            state = RuntimeState(
                **{
                    **tunnel.state.to_dict(),
                    "last_successful_probe_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            tunnel.state = state
            self.store.save_runtime(state)
        return listener, endpoint

    def process_check(self, name: str) -> CheckResult:
        state = self.store.load_runtime(name)
        if state is None:
            return CheckResult("Tunnel process", CheckStatus.FAIL, "no recorded tunnel", "NO_RUNTIME_STATE")
        if self.inspector.matches(state):
            return CheckResult("Tunnel process", CheckStatus.PASS, f"PID {state.pid} identity matches tunnel {state.tunnel_id}")
        return CheckResult("Tunnel process", CheckStatus.FAIL, "recorded PID is absent or identity does not match", "PROCESS_IDENTITY_MISMATCH")

    def disconnect(self, name: str) -> CheckResult:
        state = self.store.load_runtime(name)
        if state is None:
            return CheckResult("Tunnel process", CheckStatus.PASS, "already disconnected")
        if not self.inspector.matches(state):
            self.store.clear_runtime(name)
            return CheckResult(
                "Tunnel process",
                CheckStatus.FAIL,
                "recorded PID identity did not match; no process was terminated; stale state removed",
                "PROCESS_IDENTITY_MISMATCH",
            )
        stopped = self.inspector.terminate_owned(state, self.stop_timeout)
        if not stopped:
            return CheckResult("Tunnel process", CheckStatus.FAIL, "owned process did not exit before timeout", "PROCESS_STOP_TIMEOUT")
        self.store.clear_runtime(name)
        return CheckResult("Tunnel process", CheckStatus.PASS, f"stopped owned SSH process PID {state.pid}")

    def _wait_for_identity(self, pid: int, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            identity = self.inspector.get_identity(pid)
            if identity is not None:
                return identity
            time.sleep(0.02)
        return None

    def _wait_for_remote_identity(
        self, profile: Profile, tunnel_id: str, timeout: float
    ) -> tuple[RemoteTunnelIdentity | None, CheckResult]:
        deadline = time.monotonic() + timeout
        last_check = CheckResult(
            "Remote tunnel identity",
            CheckStatus.FAIL,
            "remote tunnel identity was not created before timeout",
            "REMOTE_IDENTITY_UNAVAILABLE",
        )
        while time.monotonic() < deadline:
            identity, last_check = self.remote_probe.read_tunnel_identity(profile, tunnel_id)
            if identity is not None:
                return identity, last_check
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        return None, last_check
