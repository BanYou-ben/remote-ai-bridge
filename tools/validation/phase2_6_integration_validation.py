"""Opt-in real-environment validation harness for the complete v0.2 backend."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
from pathlib import Path
import sys
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from app.bootstrap import AppServices, create_services
from app.domain.errors import RABError
from app.infrastructure.profile_store import ProfileNotFoundError, default_state_root
from app.services.local_proxy import DEFAULT_CANDIDATE_PORTS
from app.services.remote_port import DEFAULT_MAX_ATTEMPTS, DEFAULT_REMOTE_PORT


VALIDATION_MARKER = ".rab-phase2-6-integration-validation"
DEFAULT_API_URL = "http://127.0.0.1:8000"
PROFILE_IDENTITY_FIELDS = (
    "schema_version",
    "name",
    "profile_type",
    "host",
    "username",
    "ssh_port",
    "key_id",
    "host_key_type",
    "host_key_fingerprint",
    "local_proxy_host",
    "local_proxy_port",
    "remote_bind_host",
    "remote_port",
    "auto_reconnect",
    "endpoint_probe_url",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAB Phase 2.6 integration validation")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    _add_state_root(prepare)
    _add_ssh_target(prepare)
    _add_proxy_options(prepare)

    setup = commands.add_parser("setup")
    _add_state_root(setup)
    setup.add_argument("--name", required=True)
    _add_ssh_target(setup)
    _add_proxy_options(setup)
    setup.add_argument("--confirm-fingerprint", required=True)
    setup.add_argument("--start-remote-port", type=int, default=DEFAULT_REMOTE_PORT)
    setup.add_argument("--max-remote-port-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)

    serve_check = commands.add_parser("serve-check")
    _add_state_root(serve_check)
    serve_check.add_argument("--name", required=True)
    _add_api_url(serve_check)

    runtime_check = commands.add_parser("runtime-check")
    _add_state_root(runtime_check)
    runtime_check.add_argument("--name", required=True)
    runtime_check.add_argument("--action", choices=("connect", "status", "disconnect"), required=True)
    runtime_check.add_argument("--ready-timeout", type=float, default=60.0)
    _add_api_url(runtime_check)

    doctor_check = commands.add_parser("doctor-check")
    _add_state_root(doctor_check)
    doctor_check.add_argument("--name", required=True)
    _add_api_url(doctor_check)

    cleanup = commands.add_parser("cleanup")
    _add_state_root(cleanup)
    cleanup.add_argument("--name", required=True)
    return parser


def run(
    argv: list[str] | None = None,
    *,
    password_reader: Callable[[str], str] = getpass.getpass,
    service_factory: Callable[[Path], AppServices] = create_services,
    client_factory: Callable[..., httpx.Client] = httpx.Client,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = _validation_root(args.state_root, create=args.command == "prepare")
        if args.command == "prepare":
            return _prepare(args, root, service_factory)
        if args.command == "setup":
            password = password_reader("SSH password (input hidden): ")
            try:
                return _setup(args, root, password, service_factory)
            finally:
                # This drops only this reference; immutable strings cannot be securely erased.
                password = ""
        if args.command == "serve-check":
            return _serve_check(args, root, service_factory, client_factory)
        if args.command == "runtime-check":
            return _runtime_check(args, root, service_factory, client_factory)
        if args.command == "doctor-check":
            return _doctor_check(args, root, client_factory)
        password = password_reader("SSH password for exact validation cleanup (input hidden): ")
        try:
            return _cleanup(args, root, password, service_factory)
        finally:
            password = ""
    except RABError as exc:
        _print_json({"status": exc.code, "message": exc.message, "details": exc.details})
        return 3 if exc.retryable or exc.code.endswith("_REQUIRED") else 2
    except (httpx.HTTPError, ValueError) as exc:
        del exc
        _print_json({"status": "VALIDATION_FAILED", "message": "integration validation request failed"})
        return 2


def _prepare(args, root: Path, service_factory) -> int:
    services = service_factory(root)
    discovery = services.local_proxy.discover(
        args.endpoint_probe_url,
        candidate_ports=_candidate_ports(args),
        selected_port=args.selected_local_proxy_port,
    )
    local = {
        "selected": discovery.selected.to_dict(),
        "candidates": [item.to_dict() for item in discovery.candidates],
    }
    try:
        host = services.setup.prepare(args.host, args.username, args.port)
    except RABError as exc:
        if exc.code != "HOST_KEY_CONFIRMATION_REQUIRED":
            raise
        _print_json({"status": exc.code, "local_proxy": local, "host_key": exc.details})
        return 3
    _print_json(
        {
            "status": host.status,
            "local_proxy": local,
            "host_key": {
                "host": host.host,
                "port": host.port,
                "key_type": host.key_type,
                "fingerprint": host.fingerprint,
            },
        }
    )
    return 0


def _setup(args, root: Path, password: str, service_factory) -> int:
    services = service_factory(root)
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
    _print_json({"status": "SETUP_COMPLETE", "profile": _safe_profile(profile)})
    return 0


def _serve_check(args, root: Path, service_factory, client_factory) -> int:
    expected = service_factory(root).profiles.get(args.name)
    base_url = _validated_api_url(args.api_url)
    with client_factory(base_url=base_url, timeout=10.0) as client:
        health = _json_response(client.get("/health"))
        actual = _json_response(client.get(f"/profiles/{args.name}"))
    if health.get("status") != "ok":
        raise RABError("API_INTEGRATION_FAILED", "API health check failed")
    if not isinstance(actual, dict) or any(
        actual.get(field) != getattr(expected, field) for field in PROFILE_IDENTITY_FIELDS
    ):
        raise RABError(
            "API_STATE_ROOT_MISMATCH",
            "API profile does not match the requested validation state root",
        )
    _print_json({"status": "API_READY", "health": health, "profile": args.name})
    return 0


def _runtime_check(args, root: Path, service_factory, client_factory) -> int:
    services = service_factory(root)
    base_url = _validated_api_url(args.api_url)
    with client_factory(base_url=base_url, timeout=15.0) as client:
        if args.action == "connect":
            initial = _json_response(client.post(f"/runtime/{args.name}/connect"))
            deadline = time.monotonic() + args.ready_timeout
            current = initial
            while current.get("state") not in {"READY", "FAILED"} and time.monotonic() < deadline:
                time.sleep(0.5)
                current = _json_response(client.get(f"/runtime/{args.name}"))
            if current.get("state") != "READY":
                raise RABError("RUNTIME_VALIDATION_FAILED", "runtime did not reach READY", details=current)
            payload = current
            _require_matching_runtime(services.store.load_runtime(args.name), payload)
        elif args.action == "disconnect":
            payload = _json_response(client.post(f"/runtime/{args.name}/disconnect"))
            if payload.get("state") != "STOPPED":
                raise RABError("RUNTIME_VALIDATION_FAILED", "runtime did not stop", details=payload)
            if services.store.load_runtime(args.name) is not None:
                raise RABError("RUNTIME_EVIDENCE_REMAINS", "runtime state remained after disconnect")
        else:
            payload = _json_response(client.get(f"/runtime/{args.name}"))
            runtime = services.store.load_runtime(args.name)
            if payload.get("state") == "READY":
                _require_matching_runtime(runtime, payload)
            elif payload.get("state") in {"STOPPED", "UNSUPERVISED"} and payload.get("runtime_present") is False:
                if runtime is not None:
                    raise RABError("RUNTIME_STATE_ROOT_MISMATCH", "API runtime does not match validation state")
    _print_json({"status": "RUNTIME_CHECK_COMPLETE", "action": args.action, "runtime": payload})
    return 0


def _require_matching_runtime(runtime, snapshot: dict[str, object]) -> None:
    if runtime is None or any(
        getattr(runtime, field) != snapshot.get(field)
        for field in ("profile_name", "pid", "tunnel_id", "remote_port")
    ):
        raise RABError("RUNTIME_STATE_ROOT_MISMATCH", "API runtime does not match validation state")


def _doctor_check(args, root: Path, client_factory) -> int:
    del root
    base_url = _validated_api_url(args.api_url)
    with client_factory(base_url=base_url, timeout=60.0) as client:
        report = _json_response(client.post(f"/doctor/{args.name}"))
    _print_json({"status": "DOCTOR_COMPLETE", "report": report})
    return 0


def _cleanup(args, root: Path, password: str, service_factory) -> int:
    services = service_factory(root)
    try:
        profile = services.store.load_profile(args.name)
    except ProfileNotFoundError:
        raise RABError("VALIDATION_PROFILE_NOT_FOUND", "validation profile was not found") from None
    if profile.profile_type != "managed" or profile.schema_version != 2:
        raise RABError("VALIDATION_PROFILE_IDENTITY_MISMATCH", "profile is not a managed validation profile")
    if services.store.load_runtime(profile.name) is not None:
        raise RABError("VALIDATION_RUNTIME_ACTIVE", "disconnect the validation runtime before cleanup")

    pair = services.setup.ssh_keys.load(str(profile.key_id))
    session = services.setup.bootstrap_adapter.handshake(str(profile.host), int(profile.ssh_port))
    sftp = None
    try:
        info = session.host_key_info
        if (
            info.key_type != profile.host_key_type
            or info.fingerprint != profile.host_key_fingerprint
            or services.setup.host_keys.status(info) != "confirmed"
        ):
            raise RABError("VALIDATION_HOST_KEY_MISMATCH", "host key no longer matches the validation profile")
        services.setup.bootstrap_adapter.authenticate_password(session, str(profile.username), password)
        password = ""
        sftp = services.setup.bootstrap_adapter.open_sftp(session)
        if not services.setup.bootstrap_adapter.remove_public_key(sftp, pair.public_key):
            raise RABError(
                "VALIDATION_PUBLIC_KEY_NOT_FOUND",
                "the exact validation public key was not found; local ownership evidence was preserved",
            )
        services.setup.ssh_keys.remove_key(pair.key_id, pair.public_key)
        services.store.delete_profile(profile.name)
        if not services.setup.host_keys.remove_if_matches(info):
            raise RABError("VALIDATION_HOST_KEY_CLEANUP_FAILED", "exact validation host key cleanup failed")
    finally:
        try:
            if sftp is not None:
                sftp.close()
        except Exception:
            pass
        session.close()
        password = ""
    _remove_empty_artifacts(root, profile.name)
    _print_json({"status": "CLEANED", "name": profile.name, "key_id": pair.key_id})
    return 0


def _validation_root(value: Path, *, create: bool) -> Path:
    root = value.expanduser().resolve()
    if root == default_state_root().expanduser().resolve():
        raise RABError("VALIDATION_STATE_ROOT_UNSAFE", "validation cannot use the normal RAB state root")
    marker = root / VALIDATION_MARKER
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()) and not _regular_file(marker):
            raise RABError("VALIDATION_STATE_ROOT_UNSAFE", "non-empty root has no Phase 2.6 marker")
        if not marker.exists():
            marker.touch()
    elif not _regular_file(marker):
        raise RABError("VALIDATION_STATE_ROOT_UNSAFE", "validation root has no Phase 2.6 marker")
    return root


def _validated_api_url(value: str) -> str:
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise RABError("VALIDATION_API_URL_UNSAFE", "validation API URL has an invalid port") from None
    if (
        parsed.scheme != "http"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RABError("VALIDATION_API_URL_UNSAFE", "validation API URL must be a loopback HTTP origin")
    host = parsed.hostname
    if host == "localhost":
        pass
    else:
        try:
            if host is None or not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError:
            raise RABError("VALIDATION_API_URL_UNSAFE", "validation API URL must use loopback") from None
    return value.rstrip("/")


def _json_response(response: httpx.Response):
    if response.status_code >= 400:
        raise RABError(
            "API_INTEGRATION_FAILED",
            "API returned a non-success response",
            details={"status_code": response.status_code},
        )
    value = response.json()
    if not isinstance(value, (dict, list)):
        raise RABError("API_INTEGRATION_FAILED", "API response was not a JSON object or list")
    return value


def _safe_profile(profile) -> dict[str, object]:
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
    }


def _remove_empty_artifacts(root: Path, name: str) -> None:
    ssh_dir = root / "ssh"
    for path in (ssh_dir / "known_hosts", ssh_dir / "empty_config", ssh_dir / "empty_global_known_hosts"):
        try:
            if _regular_file(path) and path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass
    lock = root / "runtime" / f"{name}.lock"
    try:
        if _regular_file(lock) and lock.read_bytes() in {b"", b"0"}:
            lock.unlink()
    except OSError:
        pass
    for directory in (root / "ssh" / "keys", ssh_dir, root / "runtime", root / "profiles"):
        try:
            directory.rmdir()
        except OSError:
            pass
    marker = root / VALIDATION_MARKER
    try:
        if _regular_file(marker) and all(item == marker for item in root.iterdir()):
            marker.unlink()
            root.rmdir()
    except OSError:
        pass


def _candidate_ports(args) -> tuple[int, ...]:
    return tuple(args.candidate_port) if args.candidate_port else DEFAULT_CANDIDATE_PORTS


def _add_state_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", type=Path, required=True)


def _add_ssh_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--port", type=int, default=22)


def _add_proxy_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint-probe-url", default="https://api.openai.com/v1/models")
    parser.add_argument("--candidate-port", type=int, action="append")
    parser.add_argument("--selected-local-proxy-port", type=int)


def _add_api_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-url", default=DEFAULT_API_URL)


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
