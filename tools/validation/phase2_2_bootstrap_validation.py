"""Thin manual validation entrypoint for the formal Phase 2.2 SSH bootstrap.

All SSH and credential operations are delegated to production RAB modules.
This wrapper only handles CLI input, local password prompting, safe output, and
strictly scoped cleanup of its isolated validation state root.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import getpass
import json
from pathlib import Path
import sys
from typing import Callable

from app.domain.errors import RABError
from app.infrastructure.host_key_store import HostKeyStore
from app.infrastructure.profile_store import default_state_root
from app.infrastructure.ssh_bootstrap import SSHBootstrapAdapter
from app.infrastructure.ssh_key_store import SSHKeyStore
from app.services.setup_service import SetupService


VALIDATION_MARKER = ".rab-phase2-2-formal-validation"


def build_services(state_root: Path) -> tuple[SetupService, HostKeyStore, SSHKeyStore, SSHBootstrapAdapter]:
    host_keys = HostKeyStore(state_root)
    ssh_keys = SSHKeyStore(state_root)
    adapter = SSHBootstrapAdapter(state_root)
    return SetupService(host_keys, ssh_keys, adapter), host_keys, ssh_keys, adapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAB Phase 2.2 formal bootstrap validation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "bootstrap", "cleanup"):
        child = subparsers.add_parser(command)
        child.add_argument("--host", required=True)
        child.add_argument("--username", required=True)
        child.add_argument("--port", type=int, default=22)
        child.add_argument("--state-root", type=Path, required=True)
        if command == "bootstrap":
            child.add_argument("--confirm-fingerprint", required=True)
            child.add_argument("--existing-key-id")
        elif command == "cleanup":
            child.add_argument("--key-id", required=True)
    return parser


def run(
    argv: list[str] | None = None,
    *,
    password_reader: Callable[[str], str] = getpass.getpass,
    service_factory: Callable[
        [Path], tuple[SetupService, HostKeyStore, SSHKeyStore, SSHBootstrapAdapter]
    ] = build_services,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        state_root = _prepare_validation_root(args.state_root, create=args.command != "cleanup")
        setup, host_keys, ssh_keys, adapter = service_factory(state_root)
        if args.command == "prepare":
            try:
                result = setup.prepare(args.host, args.username, args.port)
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
            _print_json(asdict(result))
            return 0

        if args.command == "bootstrap":
            setup.confirm_host_key(
                args.host,
                args.port,
                args.confirm_fingerprint,
                accepted=True,
            )
            password = password_reader("SSH password (input hidden): ")
            try:
                result = setup.bootstrap(
                    args.host,
                    args.username,
                    password,
                    port=args.port,
                    confirmed_fingerprint=args.confirm_fingerprint,
                    existing_key_id=args.existing_key_id,
                )
            finally:
                password = ""
            _print_json(asdict(result))
            return 0

        return _cleanup(args, state_root, host_keys, ssh_keys, adapter, password_reader)
    except RABError as exc:
        print(f"ERROR [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2


def _cleanup(
    args: argparse.Namespace,
    state_root: Path,
    host_keys: HostKeyStore,
    ssh_keys: SSHKeyStore,
    adapter: SSHBootstrapAdapter,
    password_reader: Callable[[str], str],
) -> int:
    pair = ssh_keys.load(args.key_id)
    session = adapter.handshake(args.host, args.port)
    sftp = None
    try:
        info = session.host_key_info
        if host_keys.status(info) != "confirmed":
            raise RABError("HOST_KEY_CONFIRMATION_REQUIRED", "the validation host key is not confirmed")
        password = password_reader("SSH password for validation cleanup (input hidden): ")
        try:
            adapter.authenticate_password(session, args.username, password)
        finally:
            password = ""
        sftp = adapter.open_sftp(session)
        if not adapter.remove_public_key(sftp, pair.public_key):
            raise RABError(
                "VALIDATION_PUBLIC_KEY_NOT_FOUND",
                "the exact validation public key entry was not found; local evidence was retained",
            )
        ssh_keys.remove_key(pair.key_id, pair.public_key)
        host_keys.remove_if_matches(info)
        _remove_empty_validation_files(state_root)
        _print_json({"status": "CLEANED", "host": args.host, "port": args.port, "key_id": pair.key_id})
        return 0
    finally:
        try:
            if sftp is not None:
                sftp.close()
        except Exception:
            pass
        session.close()


def _prepare_validation_root(value: Path, *, create: bool) -> Path:
    root = value.expanduser().resolve()
    if root == default_state_root().expanduser().resolve():
        raise RABError(
            "VALIDATION_STATE_ROOT_UNSAFE",
            "validation must use a state root separate from the normal RAB state root",
        )
    marker = root / VALIDATION_MARKER
    if create:
        root.mkdir(parents=True, exist_ok=True)
        if any(root.iterdir()) and not marker.is_file():
            raise RABError(
                "VALIDATION_STATE_ROOT_UNSAFE",
                "non-empty validation state root has no RAB validation marker",
            )
        marker.touch(exist_ok=True)
    elif not marker.is_file():
        raise RABError(
            "VALIDATION_STATE_ROOT_UNSAFE",
            "cleanup requires a state root created by this validation wrapper",
        )
    return root


def _remove_empty_validation_files(root: Path) -> None:
    ssh_dir = root / "ssh"
    for path in (
        ssh_dir / "known_hosts",
        ssh_dir / "empty_config",
        ssh_dir / "empty_global_known_hosts",
    ):
        try:
            if path.is_file() and path.stat().st_size == 0:
                path.unlink()
        except OSError:
            pass
    for directory in (ssh_dir / "keys", ssh_dir):
        try:
            directory.rmdir()
        except OSError:
            pass
    marker = root / VALIDATION_MARKER
    try:
        if marker.is_file() and not any(path for path in root.iterdir() if path != marker):
            marker.unlink()
            root.rmdir()
    except OSError:
        pass


def _print_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
