from __future__ import annotations

from dataclasses import dataclass

from app.domain.errors import RABError
from app.domain.ssh_bootstrap import (
    AuthorizedKeyInstall,
    BootstrapResult,
    HostKeyInfo,
    HostPreparation,
    SSHKeyPair,
)
from app.infrastructure.host_key_store import HostKeyStore
from app.infrastructure.ssh_bootstrap import BootstrapSession, SSHBootstrapAdapter
from app.infrastructure.ssh_key_store import SSHKeyStore


@dataclass
class _SetupArtifacts:
    known_host_added: bool = False
    key_pair: SSHKeyPair | None = None
    authorized_key_install: AuthorizedKeyInstall | None = None


class SetupService:
    def __init__(
        self,
        host_keys: HostKeyStore,
        ssh_keys: SSHKeyStore,
        bootstrap: SSHBootstrapAdapter,
    ) -> None:
        self.host_keys = host_keys
        self.ssh_keys = ssh_keys
        self.bootstrap_adapter = bootstrap

    def prepare(self, host: str, username: str, port: int = 22) -> HostPreparation:
        self.bootstrap_adapter._validate_username(username)
        session = self.bootstrap_adapter.handshake(host, port)
        try:
            info = session.host_key_info
            status = self.host_keys.status(info)
            preparation = HostPreparation(host, port, info.key_type, info.fingerprint, "READY_TO_AUTH")
            if status == "unknown":
                raise self._confirmation_required(info)
            return preparation
        finally:
            session.close()

    def confirm_host_key(
        self,
        host: str,
        port: int,
        expected_fingerprint: str,
        *,
        accepted: bool,
    ) -> HostPreparation:
        session = self.bootstrap_adapter.handshake(host, port)
        try:
            info = session.host_key_info
            if not accepted:
                self.host_keys.confirm(info, accepted=False)
            if expected_fingerprint != info.fingerprint:
                raise RABError(
                    "HOST_KEY_CHANGED",
                    "the SSH host key changed after it was displayed for confirmation",
                    details={"host": host, "port": port, "fingerprint": info.fingerprint},
                )
            self.host_keys.confirm(info, accepted=True)
            return HostPreparation(host, port, info.key_type, info.fingerprint, "READY_TO_AUTH")
        finally:
            session.close()

    def bootstrap(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 22,
        confirmed_fingerprint: str | None = None,
        existing_key_id: str | None = None,
    ) -> BootstrapResult:
        artifacts = _SetupArtifacts()
        session: BootstrapSession | None = None
        sftp = None
        info: HostKeyInfo | None = None
        failure: RABError | None = None
        result: BootstrapResult | None = None
        try:
            session = self.bootstrap_adapter.handshake(host, port)
            info = session.host_key_info
            host_status = self.host_keys.status(info)
            if host_status == "unknown":
                if confirmed_fingerprint is None:
                    raise self._confirmation_required(info)
                if confirmed_fingerprint != info.fingerprint:
                    raise RABError(
                        "HOST_KEY_CHANGED",
                        "the SSH host key changed after it was displayed for confirmation",
                        details={"host": host, "port": port, "fingerprint": info.fingerprint},
                    )
                artifacts.known_host_added = self.host_keys.confirm(info, accepted=True)
            elif confirmed_fingerprint is not None and confirmed_fingerprint != info.fingerprint:
                raise RABError(
                    "HOST_KEY_CHANGED",
                    "the confirmed SSH host key does not match the server",
                    details={"host": host, "port": port, "fingerprint": info.fingerprint},
                )

            self.bootstrap_adapter.authenticate_password(session, username, password)
            artifacts.key_pair = self.ssh_keys.ensure_key(existing_key_id)
            sftp = self.bootstrap_adapter.open_sftp(session)
            artifacts.authorized_key_install = self.bootstrap_adapter.install_public_key(
                sftp, artifacts.key_pair.public_key
            )
            self.bootstrap_adapter.verify_batch_login(
                host,
                port,
                username,
                artifacts.key_pair,
                self.host_keys.path,
            )
            result = BootstrapResult(
                host,
                port,
                username,
                artifacts.key_pair.key_id,
                info.key_type,
                info.fingerprint,
                artifacts.authorized_key_install.public_key_added,
            )
        except RABError as exc:
            failure = exc
        except Exception:
            failure = RABError("SSH_BOOTSTRAP_FAILED", "SSH bootstrap failed", retryable=True)
        finally:
            # Drop the service's final password reference promptly. Python does
            # not provide reliable in-memory erasure for immutable strings.
            password = ""

        if failure is not None:
            rollback_failed = self._rollback(artifacts, info, sftp)
            self._close(sftp, session)
            if rollback_failed:
                raise RABError(
                    "SETUP_ROLLBACK_FAILED",
                    "SSH bootstrap failed and one or more owned setup artifacts could not be rolled back",
                    retryable=True,
                    details={"original_error": failure.code},
                )
            raise failure

        self._close(sftp, session)
        if result is None:
            raise RABError("SSH_BOOTSTRAP_FAILED", "SSH bootstrap did not produce a result", retryable=True)
        return result

    def _rollback(
        self,
        artifacts: _SetupArtifacts,
        info: HostKeyInfo | None,
        sftp: object | None,
    ) -> bool:
        failed = False
        pair = artifacts.key_pair
        receipt = artifacts.authorized_key_install
        if receipt is not None and pair is not None and (
            receipt.public_key_added or receipt.ssh_dir_created or receipt.authorized_keys_created
        ):
            if sftp is None:
                failed = True
            else:
                try:
                    if not self.bootstrap_adapter.rollback_public_key(sftp, pair.public_key, receipt):
                        failed = True
                except Exception:
                    failed = True
        if pair is not None and pair.generated:
            try:
                self.ssh_keys.remove_generated(pair)
            except Exception:
                failed = True
        if artifacts.known_host_added:
            if info is None:
                failed = True
            else:
                try:
                    if not self.host_keys.remove_if_matches(info):
                        failed = True
                except Exception:
                    failed = True
        return failed

    @staticmethod
    def _close(sftp: object | None, session: BootstrapSession | None) -> None:
        try:
            if sftp is not None:
                sftp.close()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            if session is not None:
                session.close()
        except Exception:
            pass

    @staticmethod
    def _confirmation_required(info: HostKeyInfo) -> RABError:
        return RABError(
            "HOST_KEY_CONFIRMATION_REQUIRED",
            "the SSH host key must be confirmed before password authentication",
            retryable=False,
            details={
                "host": info.host,
                "port": info.port,
                "key_type": info.key_type,
                "fingerprint": info.fingerprint,
            },
        )
