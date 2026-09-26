from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import sys

from app.bootstrap import create_services
from app.api.app import create_app
from app.domain.errors import RABError
from app.domain.health import CheckResult, CheckStatus
from app.domain.profile import Profile, ProfileValidationError
from app.domain.supervisor import SupervisorSnapshot, SupervisorState
from app.infrastructure.profile_store import ProfileNotFoundError
from app.redaction import redact
from app.services.doctor import DoctorService
from app.services.local_proxy import DEFAULT_CANDIDATE_PORTS, LocalProxyReport, LocalProxyService
from app.services.profile_service import ProfileService
from app.services.runtime_manager import RuntimeManager
from app.services.ssh_config import SSHConfigService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rab", description="Remote AI Bridge v0.4.0 backend CLI")
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
    get = profile_subparsers.add_parser("get", help="show one profile")
    get.add_argument("name")
    update = profile_subparsers.add_parser("update", help="update a disconnected profile")
    update.add_argument("name")
    update.add_argument("--ssh-target")
    update.add_argument("--local-proxy-port", type=int)
    update.add_argument("--remote-port", type=int)
    update.add_argument("--endpoint-probe-url")
    update.add_argument("--auto-reconnect", action=argparse.BooleanOptionalAction, default=None)
    delete = profile_subparsers.add_parser("delete", help="safely delete a profile")
    delete.add_argument("name")

    for command in ("connect", "disconnect", "status", "doctor"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("name")
    serve = subparsers.add_parser("serve", help="serve the local-only HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            return command_serve(args.state_dir, args.host, args.port)
        services = create_services(args.state_dir)
        profiles = services.profiles
        local = services.local_proxy
        ssh_config = services.ssh_config
        doctor = services.doctor
        runtime = services.runtime_manager
        if args.command == "profile":
            if args.profile_command == "add":
                return command_profile_add(args, profiles, local)
            if args.profile_command == "list":
                return command_profile_list(profiles)
            if args.profile_command == "get":
                return command_profile_get(args.name, profiles)
            if args.profile_command == "update":
                return command_profile_update(args, profiles)
            if args.profile_command == "delete":
                return command_profile_delete(args.name, profiles)
        profile = profiles.get(args.name)
        if args.command == "status":
            return command_status(profile, runtime)
        if args.command == "disconnect":
            return command_disconnect(profile, runtime)
        if args.command == "doctor":
            return command_doctor(profile, doctor)
        if args.command == "connect":
            return command_connect(profile, ssh_config, runtime)
    except RABError as exc:
        print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
        return 1 if exc.code == "PROFILE_BUSY" else 2
    except (ProfileValidationError, ProfileNotFoundError, FileExistsError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {redact(str(exc))}", file=sys.stderr)
        return 2
    return 2


def command_serve(state_dir: Path | None, host: str, port: int) -> int:
    validate_api_bind(host, port)
    import uvicorn

    uvicorn.run(
        create_app(state_dir),
        host=host,
        port=port,
        workers=1,
        reload=False,
    )
    return 0


def validate_api_bind(host: str, port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RABError("API_PORT_INVALID", "API port must be an integer from 1 to 65535")
    if not isinstance(host, str) or not host:
        raise _unsafe_api_bind()
    if host.lower() == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise _unsafe_api_bind() from None
    if not address.is_loopback:
        raise _unsafe_api_bind()


def _unsafe_api_bind() -> RABError:
    return RABError(
        "API_BIND_UNSAFE",
        "API authentication is not available; the server may only bind to a loopback address",
    )


def command_profile_add(args, profiles: ProfileService, local: LocalProxyService) -> int:
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
    profiles.create(selected)
    print(f"Saved profile '{selected.name}' with local proxy port {selected.local_proxy_port}.")
    return 0


def command_profile_list(profile_service: ProfileService) -> int:
    profiles = profile_service.list()
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


def command_profile_get(name: str, profiles: ProfileService) -> int:
    profile = profiles.get(name)
    print(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2))
    return 0


def command_profile_update(args, profiles: ProfileService) -> int:
    changes = {
        field: value
        for field, value in (
            ("ssh_target", args.ssh_target),
            ("local_proxy_port", args.local_proxy_port),
            ("remote_port", args.remote_port),
            ("endpoint_probe_url", args.endpoint_probe_url),
            ("auto_reconnect", args.auto_reconnect),
        )
        if value is not None
    }
    updated = profiles.update(args.name, changes)
    print(f"Updated profile '{updated.name}'.")
    return 0


def command_profile_delete(name: str, profiles: ProfileService) -> int:
    result = profiles.delete(name)
    if result.stopped_owned_process:
        print(f"Stopped the owned SSH tunnel for profile '{name}'.")
    elif result.removed_stale_runtime:
        print(f"Removed stale runtime for profile '{name}' without terminating an unverified process.")
    print(f"Deleted profile '{name}'.")
    return 0


def command_status(profile: Profile, runtime: RuntimeManager) -> int:
    snapshot = runtime.status(profile.name)
    print_supervisor_snapshot(snapshot)
    if snapshot.state is SupervisorState.READY:
        return 0
    if snapshot.state is SupervisorState.UNSUPERVISED and snapshot.process_alive:
        return 0
    return 1


def command_disconnect(profile: Profile, runtime: RuntimeManager) -> int:
    result = runtime.stop(profile.name)
    print_supervisor_snapshot(result.snapshot)
    return 0 if result.snapshot.state is SupervisorState.STOPPED else 1


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
    ssh_config: SSHConfigService,
    runtime: RuntimeManager,
) -> int:
    ssh_check = ssh_config.check(profile)
    print_check(ssh_check)
    if not ssh_check.passed:
        return 1
    previous: SupervisorSnapshot | None = None
    try:
        while True:
            snapshot = runtime.start(profile.name) if previous is None else runtime.wait(profile.name, 0.25)
            if previous is None or snapshot != previous:
                print_supervisor_snapshot(snapshot)
                previous = snapshot
            if snapshot.state is SupervisorState.FAILED:
                return 1
            if snapshot.state is SupervisorState.STOPPED:
                return 0
    except KeyboardInterrupt:
        print("\nStopping foreground supervision...")
        result = runtime.stop(profile.name)
        print_supervisor_snapshot(result.snapshot)
        return 0 if result.worker_exited and result.snapshot.state is SupervisorState.STOPPED else 1


def print_local_report(report: LocalProxyReport) -> None:
    print_check(report.tcp)
    print_check(report.handshake)
    print_check(report.endpoint)


def print_check(check: CheckResult) -> None:
    code = f" [{check.error_code}]" if check.error_code else ""
    print(f"{check.name}: {check.status.value}{code} - {redact(check.detail)}")


def print_supervisor_snapshot(snapshot: SupervisorSnapshot) -> None:
    code = f" [{snapshot.error_code}]" if snapshot.error_code else ""
    retry = f"; retry in {snapshot.retry_in_seconds:g}s" if snapshot.retry_in_seconds is not None else ""
    suggestion = f"; suggested remote port {snapshot.suggested_port}" if snapshot.suggested_port is not None else ""
    print(f"{snapshot.state.value}{code}: {redact(snapshot.message)}{retry}{suggestion}")


if __name__ == "__main__":
    raise SystemExit(main())
