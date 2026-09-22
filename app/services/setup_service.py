from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, cast

from app.domain.errors import RABError
from app.domain.profile import Profile, ProfileValidationError, validate_managed_profile_setup_inputs
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
from app.services.local_proxy import DEFAULT_CANDIDATE_PORTS, LocalProxyService
from app.services.profile_service import ProfileService
from app.services.remote_port import DEFAULT_MAX_ATTEMPTS, DEFAULT_REMOTE_PORT, RemotePortSelector


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
        *,
        local_proxy: LocalProxyService | None = None,
        remote_ports: RemotePortSelector | None = None,
        profiles: ProfileService | None = None,
    ) -> None:
        self.host_keys = host_keys
        self.ssh_keys = ssh_keys
        self.bootstrap_adapter = bootstrap
        self.local_proxy = local_proxy
        self.remote_ports = remote_ports
        self.profiles = profiles

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
        return cast(
            BootstrapResult,
            self._run_bootstrap(
                host,
                username,
                password,
                port=port,
                confirmed_fingerprint=confirmed_fingerprint,
                existing_key_id=existing_key_id,
            ),
        )

    def setup_managed_profile(
        self,
        name: str,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 22,
        confirmed_fingerprint: str | None = None,
        existing_key_id: str | None = None,
        selected_local_proxy_port: int | None = None,
        candidate_proxy_ports: tuple[int, ...] = DEFAULT_CANDIDATE_PORTS,
        start_remote_port: int = DEFAULT_REMOTE_PORT,
        max_remote_port_attempts: int = DEFAULT_MAX_ATTEMPTS,
        auto_reconnect: bool = True,
        endpoint_probe_url: str = "https://api.openai.com/v1/models",
    ) -> Profile:
        local_proxy, remote_ports, profiles = self._setup_components()
        try:
            validate_managed_profile_setup_inputs(
                name=name,
                host=host,
                username=username,
                ssh_port=port,
                auto_reconnect=auto_reconnect,
                endpoint_probe_url=endpoint_probe_url,
            )
        except ProfileValidationError as exc:
            raise RABError("PROFILE_INVALID", str(exc), details={"name": name}) from exc
        local_proxy.validate_discovery_inputs(
            endpoint_probe_url,
            candidate_ports=candidate_proxy_ports,
        )
        remote_ports.validate_scan_parameters(
            start_port=start_remote_port,
            max_attempts=max_remote_port_attempts,
        )
        profiles.ensure_create_available(name)

        # Resolve ambiguous discovery before creating credentials. The selected
        # endpoint is checked again after bootstrap to close the preflight race.
        preflight = local_proxy.discover(
            endpoint_probe_url,
            candidate_ports=candidate_proxy_ports,
            selected_port=selected_local_proxy_port,
        )
        selected = preflight.selected

        def complete(bootstrap_result: BootstrapResult) -> Profile:
            verified = local_proxy.discover(
                endpoint_probe_url,
                candidate_ports=candidate_proxy_ports,
                selected_port=selected.port,
            ).selected
            provisional = Profile(
                schema_version=2,
                name=name,
                ssh_target=f"{username}@{host}",
                local_proxy_host=verified.host,
                local_proxy_port=verified.port,
                remote_bind_host="127.0.0.1",
                remote_port=start_remote_port,
                auto_reconnect=auto_reconnect,
                endpoint_probe_url=endpoint_probe_url,
                profile_type="managed",
                host=host,
                username=username,
                ssh_port=port,
                key_id=bootstrap_result.key_id,
                host_key_type=bootstrap_result.host_key_type,
                host_key_fingerprint=bootstrap_result.host_key_fingerprint,
            )
            provisional.validate()
            selected_remote_port = remote_ports.select(
                provisional,
                start_port=start_remote_port,
                max_attempts=max_remote_port_attempts,
            )
            profile = Profile.from_dict({**provisional.to_dict(), "remote_port": selected_remote_port})
            return profiles.create(profile)

        return cast(
            Profile,
            self._run_bootstrap(
                host,
                username,
                password,
                port=port,
                confirmed_fingerprint=confirmed_fingerprint,
                existing_key_id=existing_key_id,
                completion=complete,
            ),
        )

    def _run_bootstrap(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int,
        confirmed_fingerprint: str | None,
        existing_key_id: str | None,
        completion: Callable[[BootstrapResult], object] | None = None,
    ) -> object:
        artifacts = _SetupArtifacts()
        session: BootstrapSession | None = None
        sftp = None
        info: HostKeyInfo | None = None
        failure: RABError | None = None
        result: object | None = None
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
            password = ""
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
            bootstrap_result = BootstrapResult(
                host,
                port,
                username,
                artifacts.key_pair.key_id,
                info.key_type,
                info.fingerprint,
                artifacts.authorized_key_install.public_key_added,
            )
            if completion is None:
                result = bootstrap_result
            else:
                try:
                    result = completion(bootstrap_result)
                except RABError:
                    raise
                except Exception:
                    raise RABError(
                        "MANAGED_SETUP_FAILED",
                        "managed profile setup failed after SSH bootstrap completed",
                        retryable=True,
                    ) from None
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

    def _setup_components(self) -> tuple[LocalProxyService, RemotePortSelector, ProfileService]:
        if self.local_proxy is None or self.remote_ports is None or self.profiles is None:
            raise RABError(
                "MANAGED_SETUP_UNAVAILABLE",
                "managed profile setup discovery services are not configured",
            )
        return self.local_proxy, self.remote_ports, self.profiles

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
