from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import re
import sys
import time

from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile, ProfileValidationError
from app.infrastructure.process_identity import ProcessInspector
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.profile_store import ProfileLockError, ProfileNotFoundError, ProfileStore
from app.services.doctor import DoctorService
from app.services.local_proxy import DEFAULT_CANDIDATE_PORTS, LocalProxyReport, LocalProxyService
from app.services.remote_probe import RemoteProbeService
from app.services.ssh_config import SSHConfigService, locate_ssh
from app.services.tunnel import ActiveTunnel, RemotePortConflictError, TunnelError, TunnelManager


BACKOFF_SECONDS = (1, 2, 5, 10, 30)
HEALTH_INTERVAL_SECONDS = 15.0
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*)([^\s]+(?:\s+[^\s]+)?)"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s]+"),
    re.compile(r"(?i)((?:access[_-]?token|refresh[_-]?token|token|secret)\s*[=:]\s*)[^\s&]+"),
    re.compile(r"(?i)((?:cookie|set-cookie)\s*:\s*)[^\r\n]+"),
    re.compile(r"(?i)(https?://[^:/\s]+:)[^@\s]+@"),
    re.compile(
        r"(?is)(-----BEGIN [^-\r\n]*PRIVATE KEY-----).*?(-----END [^-\r\n]*PRIVATE KEY-----)"
    ),
)


def redact(value: str) -> str:
    redacted = value
    for pattern in SECRET_PATTERNS:
        if "PRIVATE KEY" in pattern.pattern:
            redacted = pattern.sub(r"\1\n[REDACTED]\n\2", redacted)
        else:
            redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rab", description="Remote AI Bridge Phase 1 CLI prototype")
    parser.add_argument("--state-dir", type=Path, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile_parser = subparsers.add_parser("profile", help="manage server profiles")
    profile_subparsers = profile_parser.add_subparsers(dest="profile_command", required=True)
    add = profile_subparsers.add_parser("add", help="validate and add a profile")
    add.add_argument("name")
    add.add_argument("--ssh-target")
    add.add_argument("--local-proxy-port", type=int)
    add.add_argument("--remote-port", type=int, default=17890)
    add.add_argument("--endpoint-probe-url", default="https://api.openai.com/v1/models")
    add.add_argument("--auto-reconnect", action=argparse.BooleanOptionalAction, default=True)
    profile_subparsers.add_parser("list", help="list profiles")

    for command in ("connect", "disconnect", "status", "doctor"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("name")
    return parser


def make_services(state_dir: Path | None = None):
    store = ProfileStore(state_dir)
    runner = ProcessRunner()
    inspector = ProcessInspector()
    ssh_executable = locate_ssh()
    if ssh_executable is None:
        raise RuntimeError("Windows OpenSSH ssh.exe was not found on PATH")
    local = LocalProxyService()
    remote = RemoteProbeService(runner, ssh_executable)
    ssh_config = SSHConfigService(runner, ssh_executable)
    tunnel = TunnelManager(runner, inspector, store, remote, ssh_executable)
    doctor = DoctorService(local, ssh_config, tunnel, remote)
    return store, local, ssh_config, remote, tunnel, doctor


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store, local, ssh_config, remote, tunnel, doctor = make_services(args.state_dir)
        if args.command == "profile":
            if args.profile_command == "add":
                return command_profile_add(args, store, local)
            return command_profile_list(store)
        profile = store.load_profile(args.name)
        if args.command == "status":
            return command_status(profile, store, tunnel)
        if args.command == "disconnect":
            try:
                with store.supervisor_lock(profile.name):
                    return command_disconnect(profile, tunnel)
            except ProfileLockError:
                print(
                    "ERROR: the foreground supervisor is active; press Ctrl+C in its terminal to stop supervision and the tunnel",
                    file=sys.stderr,
                )
                return 1
        if args.command == "doctor":
            return command_doctor(profile, doctor)
        if args.command == "connect":
            try:
                with store.supervisor_lock(profile.name):
                    return command_connect(profile, store, local, ssh_config, tunnel)
            except ProfileLockError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
    except (ProfileValidationError, ProfileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {redact(str(exc))}", file=sys.stderr)
        return 2
    return 2


def command_profile_add(args, store: ProfileStore, local: LocalProxyService) -> int:
    candidate_ports = (args.local_proxy_port,) if args.local_proxy_port is not None else DEFAULT_CANDIDATE_PORTS
    selected: Profile | None = None
    for port in candidate_ports:
        profile = Profile(
            schema_version=1,
            name=args.name,
            ssh_target=args.ssh_target or args.name,
            local_proxy_port=port,
            remote_port=args.remote_port,
            auto_reconnect=args.auto_reconnect,
            endpoint_probe_url=args.endpoint_probe_url,
        )
        profile.validate()
        print(f"Local proxy candidate 127.0.0.1:{port}")
        report = local.inspect(profile)
        print_local_report(report)
        if report.passed and selected is None:
            selected = profile
            if args.local_proxy_port is not None:
                break
    if selected is None:
        print("ERROR: no candidate passed TCP, HTTP proxy handshake, and AI endpoint checks", file=sys.stderr)
        return 1
    store.save_profile(selected)
    print(f"Saved profile '{selected.name}' with local proxy port {selected.local_proxy_port}.")
    return 0


def command_profile_list(store: ProfileStore) -> int:
    profiles = store.list_profiles()
    if not profiles:
        print("No profiles.")
        return 0
    print("NAME\tSSH TARGET\tLOCAL PROXY\tREMOTE ENDPOINT\tAUTO RECONNECT")
    for profile in profiles:
        print(
            f"{profile.name}\t{profile.ssh_target}\t{profile.local_proxy_host}:{profile.local_proxy_port}"
            f"\t{profile.remote_bind_host}:{profile.remote_port}\t{str(profile.auto_reconnect).lower()}"
        )
    return 0


def command_status(profile: Profile, store: ProfileStore, tunnel: TunnelManager) -> int:
    state = store.load_runtime(profile.name)
    check = tunnel.process_check(profile.name)
    if state is None:
        print("Disconnected: no recorded tunnel state.")
        return 1
    print_check(check)
    if check.passed:
        print(
            f"Recorded tunnel: pid={state.pid}, tunnel_id={state.tunnel_id}, "
            f"remote_port={state.remote_port}, last_probe={state.last_successful_probe_at or 'never'}"
        )
        return 0
    print("Disconnected: recorded process identity is stale or mismatched.")
    return 1


def command_disconnect(profile: Profile, tunnel: TunnelManager) -> int:
    result = tunnel.disconnect(profile.name)
    print_check(result)
    return 0 if result.passed else 1


def command_doctor(profile: Profile, doctor: DoctorService) -> int:
    report = doctor.run(profile)
    checks = (
        report.local.tcp,
        report.local.handshake,
        report.local.endpoint,
        report.ssh,
        report.tunnel,
        report.remote_listener,
        report.remote_endpoint,
    )
    for check in checks:
        print_check(check)
    return 0 if all(check.status in (CheckStatus.PASS, CheckStatus.SKIP) for check in checks) else 1


def command_connect(
    profile: Profile,
    store: ProfileStore,
    local: LocalProxyService,
    ssh_config: SSHConfigService,
    tunnel_manager: TunnelManager,
) -> int:
    ssh_check = ssh_config.check(profile.ssh_target)
    print_check(ssh_check)
    if not ssh_check.passed:
        return 1

    active: ActiveTunnel | None = None
    backoff_index = 0
    try:
        while True:
            if active is not None and active.poll() is not None:
                print(
                    "Degraded: owned SSH process exited; retaining runtime so any stale remote session "
                    "can be identity-verified before replacement."
                )
                active = None

            local_report = local.inspect(profile)
            print_local_report(local_report)
            if not local_report.tcp.passed or not local_report.handshake.passed:
                if not profile.auto_reconnect:
                    print("Degraded: local proxy failure.")
                    return 1
                delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
                backoff_index += 1
                print(f"Degraded: local proxy failure; retrying in {delay}s. Ctrl+C to stop.")
                time.sleep(delay)
                continue
            if not local_report.endpoint.passed:
                if not profile.auto_reconnect:
                    print("Degraded: network/endpoint path unhealthy.")
                    return 1
                delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
                backoff_index += 1
                print(f"Degraded: network/endpoint path unhealthy; retrying in {delay}s. Ctrl+C to stop.")
                time.sleep(delay)
                continue

            if active is None:
                try:
                    active = tunnel_manager.acquire(profile)
                except RemotePortConflictError as exc:
                    print(f"ERROR [{exc.error_code}]: {exc}", file=sys.stderr)
                    if exc.suggested_port is None or not _confirm_port_change(exc.suggested_port):
                        return 1
                    profile = replace(profile, remote_port=exc.suggested_port)
                    profile.validate()
                    store.save_profile(profile, overwrite=True)
                    print(f"Updated profile '{profile.name}' to remote port {profile.remote_port} after confirmation.")
                    continue
                except TunnelError as exc:
                    print(f"Degraded [{exc.error_code}]: {redact(str(exc))}")
                    if not profile.auto_reconnect:
                        return 1
                    delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
                    backoff_index += 1
                    print(f"Retrying in {delay}s. Ctrl+C to stop.")
                    time.sleep(delay)
                    continue

            listener, endpoint = tunnel_manager.verify(profile, active)
            print_check(listener)
            print_check(endpoint)
            if not listener.passed or not endpoint.passed:
                if active.poll() is not None:
                    print(
                        "Degraded: owned SSH process exited; retaining runtime so any stale remote session "
                        "can be identity-verified before replacement."
                    )
                    active = None
                else:
                    print("Degraded: tunnel health check failed; keeping the live owned SSH process for recovery.")
                if not profile.auto_reconnect:
                    if active is not None:
                        if not active.stop(tunnel_manager.stop_timeout):
                            print(
                                "ERROR: failed to stop the identity-verified tunnel; runtime state was retained.",
                                file=sys.stderr,
                            )
                            return 1
                        store.clear_runtime(profile.name)
                        active = None
                    return 1
                delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
                backoff_index += 1
                print(f"Rechecking current tunnel in {delay}s. Ctrl+C to stop.")
                time.sleep(delay)
                continue

            backoff_index = 0
            print("Ready: bridge is healthy. Foreground supervision is active; press Ctrl+C to disconnect.")
            last_health_check = time.monotonic()
            while active.poll() is None:
                time.sleep(0.5)
                if time.monotonic() - last_health_check >= HEALTH_INTERVAL_SECONDS:
                    listener, endpoint = tunnel_manager.verify(profile, active)
                    if not listener.passed or not endpoint.passed:
                        print("Degraded: bridge health check failed; retaining the current owned tunnel.")
                        break
                    last_health_check = time.monotonic()
            if active.poll() is not None:
                print(
                    "Degraded: owned SSH process exited; retaining runtime so any stale remote session "
                    "can be identity-verified before replacement."
                )
                active = None
            if not profile.auto_reconnect:
                if active is not None:
                    if not active.stop(tunnel_manager.stop_timeout):
                        print(
                            "ERROR: failed to stop the identity-verified tunnel; runtime state was retained.",
                            file=sys.stderr,
                        )
                        return 1
                    store.clear_runtime(profile.name)
                    active = None
                return 1
            delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
            backoff_index += 1
            action = "Rechecking current tunnel" if active is not None else "Reconnecting with a new tunnel"
            print(f"{action} in {delay}s. Ctrl+C to stop.")
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\nStopping foreground supervision...")
        if active is not None:
            if active.stop(tunnel_manager.stop_timeout):
                store.clear_runtime(profile.name)
                print("Owned SSH tunnel stopped.")
            else:
                print("WARNING: process identity did not match or the owned process did not stop; no unrelated process was terminated.")
                return 1
        else:
            state = store.load_runtime(profile.name)
            if state is not None and tunnel_manager.inspector.matches(state):
                result = tunnel_manager.disconnect(profile.name)
                print_check(result)
                return 0 if result.passed else 1
        return 0


def print_local_report(report: LocalProxyReport) -> None:
    print_check(report.tcp)
    print_check(report.handshake)
    print_check(report.endpoint)


def print_check(check: CheckResult) -> None:
    code = f" [{check.error_code}]" if check.error_code else ""
    print(f"{check.name}: {check.status.value}{code} - {redact(check.detail)}")


def _confirm_port_change(port: int) -> bool:
    try:
        answer = input(f"Use remote loopback port {port} and update the profile? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


if __name__ == "__main__":
    raise SystemExit(main())
