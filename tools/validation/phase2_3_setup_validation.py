"""Thin real-environment validator for the formal Phase 2.3 setup flow."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import getpass
import json
from pathlib import Path
import sys
from typing import Callable

from app.domain.errors import RABError
from app.domain.profile import Profile
from app.infrastructure.host_key_store import HostKeyStore
from app.infrastructure.process_identity import ProcessInspector
from app.infrastructure.process_runner import ProcessRunner
from app.infrastructure.profile_store import ProfileNotFoundError, ProfileStore, default_state_root
from app.infrastructure.ssh_bootstrap import SSHBootstrapAdapter
from app.infrastructure.ssh_key_store import SSHKeyStore
from app.services.local_proxy import DEFAULT_CANDIDATE_PORTS, LocalProxyService
from app.services.profile_service import ProfileService
from app.services.remote_port import DEFAULT_MAX_ATTEMPTS, DEFAULT_REMOTE_PORT, RemotePortSelector
from app.services.remote_probe import RemoteProbeService
from app.services.setup_service import SetupService
from app.services.ssh_config import locate_ssh
from app.services.tunnel import TunnelManager


VALIDATION_MARKER = ".rab-phase2-3-formal-validation"


@dataclass(frozen=True)
class ValidationServices:
    setup: SetupService
    local_proxy: LocalProxyService
    store: ProfileStore
    profiles: ProfileService
    host_keys: HostKeyStore
    ssh_keys: SSHKeyStore
    bootstrap: SSHBootstrapAdapter


def build_services(state_root: Path) -> ValidationServices:
    ssh_executable = locate_ssh()
    if ssh_executable is None:
        raise RABError("SSH_NOT_FOUND", "Windows OpenSSH ssh.exe was not found")
    store = ProfileStore(state_root)
    runner = ProcessRunner()
    inspector = ProcessInspector()
    local_proxy = LocalProxyService()
    remote_probe = RemoteProbeService(runner, ssh_executable, state_root=state_root)
    tunnel = TunnelManager(runner, inspector, store, remote_probe, ssh_executable)
    profiles = ProfileService(store, tunnel)
    host_keys = HostKeyStore(state_root)
    ssh_keys = SSHKeyStore(state_root, runner=runner)
    bootstrap = SSHBootstrapAdapter(state_root, runner=runner, ssh_executable=ssh_executable)
    remote_ports = RemotePortSelector(remote_probe)
    setup = SetupService(
        host_keys,
        ssh_keys,
        bootstrap,
        local_proxy=local_proxy,
        remote_ports=remote_ports,
        profiles=profiles,
    )
    return ValidationServices(setup, local_proxy, store, profiles, host_keys, ssh_keys, bootstrap)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAB Phase 2.3 formal setup validation")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    _add_ssh_target(prepare)
    _add_state_root(prepare)

    discover = commands.add_parser("discover")
    _add_state_root(discover)
    _add_proxy_options(discover)

    setup = commands.add_parser("setup")
    setup.add_argument("--name", required=True)
    _add_ssh_target(setup)
    _add_state_root(setup)
    setup.add_argument("--confirm-fingerprint", required=True)
    _add_proxy_options(setup)
    setup.add_argument("--start-remote-port", type=int, default=DEFAULT_REMOTE_PORT)
    setup.add_argument("--max-remote-port-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--name", required=True)
    _add_state_root(inspect)

    cleanup = commands.add_parser("cleanup")
    cleanup.add_argument("--name", required=True)
    _add_ssh_target(cleanup)
    _add_state_root(cleanup)
    return parser


def run(
    argv: list[str] | None = None,
    *,
    password_reader: Callable[[str], str] = getpass.getpass,
    service_factory: Callable[[Path], ValidationServices] = build_services,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = _prepare_validation_root(
            args.state_root,
            create=args.command in {"prepare", "discover", "setup"},
        )
        services = service_factory(root)
        if args.command == "prepare":
            return _prepare(args, services)
        if args.command == "discover":
            return _discover(args, services)
        if args.command == "setup":
            password = password_reader("SSH password (input hidden): ")
            try:
                return _setup(args, password, services)
            finally:
                # This only drops the current binding; Python strings cannot be reliably erased.
                password = ""
        if args.command == "inspect":
            profile = services.profiles.get(args.name)
            _print_json({"status": "PROFILE_LOADED", **_safe_profile(profile)})
            return 0
        return _cleanup(args, root, services, password_reader)
    except RABError as exc:
        if exc.code == "LOCAL_PROXY_SELECTION_REQUIRED":
            _print_json({"status": exc.code, "candidates": exc.details.get("candidates", [])})
            return 3
        print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except ProfileNotFoundError:
        print("ERROR [VALIDATION_PROFILE_NOT_FOUND]: validation profile was not found", file=sys.stderr)
        return 2


def _prepare(args: argparse.Namespace, services: ValidationServices) -> int:
    try:
        result = services.setup.prepare(args.host, args.username, args.port)
    except RABError as exc:
        if exc.code != "HOST_KEY_CONFIRMATION_REQUIRED":
            raise
        _print_json(
            {
                "status": exc.code,
                "host": exc.details["host"],
                "port": exc.details["port"],
                "key_type": exc.details["key_type"],
                "fingerprint": exc.details["fingerprint"],
            }
        )
        return 3
    _print_json({"status": result.status, **asdict(result)})
    return 0


def _discover(args: argparse.Namespace, services: ValidationServices) -> int:
    result = services.local_proxy.discover(
        args.endpoint_probe_url,
        candidate_ports=_candidate_ports(args),
        selected_port=args.selected_local_proxy_port,
    )
    _print_json(
        {
            "status": "LOCAL_PROXY_SELECTED",
            "selected": result.selected.to_dict(),
            "candidates": [candidate.to_dict() for candidate in result.candidates],
        }
    )
    return 0


def _setup(args: argparse.Namespace, password: str, services: ValidationServices) -> int:
    profile = services.setup.setup_managed_profile(
        args.name,
        args.host,
        args.username,
        password,
        port=args.port,
        confirmed_fingerprint=args.confirm_fingerprint,
        selected_local_proxy_port=args.selected_local_proxy_port,
        candidate_proxy_ports=_candidate_ports(args),
        start_remote_port=args.start_remote_port,
        max_remote_port_attempts=args.max_remote_port_attempts,
        endpoint_probe_url=args.endpoint_probe_url,
    )
    saved = services.store.load_profile(profile.name)
    if saved != profile:
        raise RABError(
            "VALIDATION_PROFILE_PERSISTENCE_MISMATCH",
            "saved validation profile does not match the setup result",
        )
    _print_json({"status": "SETUP_COMPLETE", **_safe_profile(saved)})
    return 0


def _cleanup(
    args: argparse.Namespace,
    root: Path,
    services: ValidationServices,
    password_reader: Callable[[str], str],
) -> int:
    try:
        profile = services.store.load_profile(args.name)
    except ProfileNotFoundError:
        raise RABError("VALIDATION_PROFILE_NOT_FOUND", "validation profile was not found") from None
    if (
        profile.schema_version != 2
        or profile.profile_type != "managed"
        or profile.host != args.host
        or profile.username != args.username
        or profile.ssh_port != args.port
    ):
        raise RABError(
            "VALIDATION_PROFILE_IDENTITY_MISMATCH",
            "validation profile identity does not match the requested cleanup target",
        )
    pair = services.ssh_keys.load(str(profile.key_id))
    session = services.bootstrap.handshake(args.host, args.port)
    sftp = None
    try:
        info = session.host_key_info
        if (
            info.key_type != profile.host_key_type
            or info.fingerprint != profile.host_key_fingerprint
            or services.host_keys.status(info) != "confirmed"
        ):
            raise RABError(
                "VALIDATION_HOST_KEY_MISMATCH",
                "current SSH host key does not match the validation profile",
            )
        password = password_reader("SSH password for validation cleanup (input hidden): ")
        try:
            services.bootstrap.authenticate_password(session, args.username, password)
        finally:
            password = ""
        sftp = services.bootstrap.open_sftp(session)
        if not services.bootstrap.remove_public_key(sftp, pair.public_key):
            raise RABError(
                "VALIDATION_PUBLIC_KEY_NOT_FOUND",
                "the exact validation public key was not found; local evidence was preserved",
            )
        services.ssh_keys.remove_key(pair.key_id, pair.public_key)
        services.store.delete_profile(profile.name)
        if not services.host_keys.remove_if_matches(info):
            raise RABError(
                "VALIDATION_HOST_KEY_CLEANUP_FAILED",
                "the exact validation host key entry could not be removed",
            )
    finally:
        try:
            if sftp is not None:
                sftp.close()
        except Exception:
            pass
        session.close()
    _remove_empty_validation_artifacts(root, profile.name)
    _print_json({"status": "CLEANED", "name": profile.name, "key_id": pair.key_id})
    return 0


def _prepare_validation_root(value: Path, *, create: bool) -> Path:
    root = value.expanduser().resolve()
    if root == default_state_root().expanduser().resolve():
        raise RABError("VALIDATION_STATE_ROOT_UNSAFE", "validation must not use the normal RAB state root")
    marker = root / VALIDATION_MARKER
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()) and not _is_regular_file(marker):
            raise RABError(
                "VALIDATION_STATE_ROOT_UNSAFE",
                "non-empty validation state root has no Phase 2.3 validation marker",
            )
        if not marker.exists():
            marker.touch()
    elif not _is_regular_file(marker):
        raise RABError(
            "VALIDATION_STATE_ROOT_UNSAFE",
            "validation state root has no Phase 2.3 validation marker",
        )
    return root


def _remove_empty_validation_artifacts(root: Path, name: str) -> None:
    ssh_dir = root / "ssh"
    for path in (
        ssh_dir / "known_hosts",
        ssh_dir / "empty_config",
        ssh_dir / "empty_global_known_hosts",
    ):
        try:
            if _is_regular_file(path) and path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass
    lock = root / "runtime" / f"{name}.lock"
    try:
        if _is_regular_file(lock) and lock.read_bytes() in {b"", b"0"}:
            lock.unlink()
    except OSError:
        pass
    for directory in (
        root / "ssh" / "keys",
        root / "ssh",
        root / "runtime",
        root / "profiles",
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    marker = root / VALIDATION_MARKER
    try:
        if _is_regular_file(marker) and all(path == marker for path in root.iterdir()):
            marker.unlink()
            root.rmdir()
    except OSError:
        pass


def _safe_profile(profile: Profile) -> dict[str, object]:
    return {
        "name": profile.name,
        "host": profile.host,
        "username": profile.username,
        "ssh_port": profile.ssh_port,
        "key_id": profile.key_id,
        "host_key_type": profile.host_key_type,
        "host_key_fingerprint": profile.host_key_fingerprint,
        "local_proxy_host": profile.local_proxy_host,
        "local_proxy_port": profile.local_proxy_port,
        "remote_bind_host": profile.remote_bind_host,
        "remote_port": profile.remote_port,
        "auto_reconnect": profile.auto_reconnect,
        "endpoint_probe_url": profile.endpoint_probe_url,
    }


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _candidate_ports(args: argparse.Namespace) -> tuple[int, ...]:
    return tuple(args.candidate_port) if args.candidate_port else DEFAULT_CANDIDATE_PORTS


def _add_ssh_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--port", type=int, default=22)


def _add_state_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", type=Path, required=True)


def _add_proxy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint-probe-url", default="https://api.openai.com/v1/models")
    parser.add_argument("--candidate-port", type=int, action="append")
    parser.add_argument("--selected-local-proxy-port", type=int)


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
