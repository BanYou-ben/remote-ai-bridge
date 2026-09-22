from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from app.domain.errors import RABError
from app.domain.profile import Profile, ProfileValidationError, RuntimeState, validate_profile_name
from app.infrastructure.profile_store import ProfileLockError, ProfileNotFoundError, ProfileStore
from app.services.tunnel import TunnelManager


UPDATABLE_PROFILE_FIELDS = frozenset(
    {
        "ssh_target",
        "local_proxy_port",
        "remote_port",
        "auto_reconnect",
        "endpoint_probe_url",
    }
)

# The foreground supervisor holds a snapshot of the complete profile. Until the
# reusable supervisor exists, no persisted field can be guaranteed to take
# effect online. All updates therefore require the profile lock.
ONLINE_SAFE_PROFILE_FIELDS = frozenset()


@dataclass(frozen=True)
class ProfileDeleteResult:
    name: str
    stopped_owned_process: bool = False
    removed_stale_runtime: bool = False


class ProfileService:
    def __init__(self, store: ProfileStore, tunnel: TunnelManager) -> None:
        self.store = store
        self.tunnel = tunnel

    def list(self) -> list[Profile]:
        return self.store.list_profiles()

    def get(self, name: str) -> Profile:
        try:
            return self.store.load_profile(name)
        except ProfileNotFoundError as exc:
            raise RABError("PROFILE_NOT_FOUND", str(exc), details={"name": name}) from exc

    def create(self, profile: Profile) -> Profile:
        try:
            profile.validate()
            with self.store.supervisor_lock(profile.name):
                self.store.save_profile(profile)
        except ProfileLockError as exc:
            raise self._busy(profile.name, exc) from exc
        except FileExistsError as exc:
            raise RABError("PROFILE_EXISTS", str(exc), details={"name": profile.name}) from exc
        except ProfileValidationError as exc:
            raise RABError("PROFILE_INVALID", str(exc), details={"name": profile.name}) from exc
        return profile

    def ensure_create_available(self, name: str) -> None:
        try:
            validate_profile_name(name)
            with self.store.supervisor_lock(name):
                if self.store.profile_exists(name):
                    raise RABError(
                        "PROFILE_EXISTS",
                        f"profile already exists: {name}",
                        details={"name": name},
                    )
        except ProfileLockError as exc:
            raise self._busy(name, exc) from exc
        except ProfileValidationError as exc:
            raise RABError("PROFILE_INVALID", str(exc), details={"name": name}) from exc

    def update(self, name: str, changes: Mapping[str, Any]) -> Profile:
        if not changes:
            raise RABError("PROFILE_UPDATE_EMPTY", "no profile fields were provided", details={"name": name})
        invalid_fields = sorted(set(changes) - UPDATABLE_PROFILE_FIELDS)
        if invalid_fields:
            raise RABError(
                "PROFILE_FIELD_NOT_UPDATABLE",
                f"profile fields cannot be updated: {', '.join(invalid_fields)}",
                details={"name": name, "fields": invalid_fields},
            )
        try:
            with self.store.supervisor_lock(name):
                current = self.store.load_profile(name)
                if self.store.load_runtime(name) is not None:
                    raise RABError(
                        "PROFILE_RUNTIME_ACTIVE",
                        "profile runtime exists; safely disconnect or clean it up before updating the profile",
                        retryable=True,
                        details={"name": name},
                    )
                updated = replace(current, **dict(changes))
                updated.validate()
                self.store.update_profile(updated)
                return updated
        except ProfileLockError as exc:
            raise self._busy(name, exc) from exc
        except ProfileNotFoundError as exc:
            raise RABError("PROFILE_NOT_FOUND", str(exc), details={"name": name}) from exc
        except ProfileValidationError as exc:
            raise RABError("PROFILE_INVALID", str(exc), details={"name": name}) from exc

    def delete(self, name: str) -> ProfileDeleteResult:
        try:
            with self.store.supervisor_lock(name):
                profile = self.store.load_profile(name)
                if profile.schema_version == 2 or profile.profile_type == "managed":
                    raise RABError(
                        "MANAGED_PROFILE_CREDENTIAL_CLEANUP_REQUIRED",
                        "managed profile credentials must be safely revoked before the profile can be deleted",
                        details={"name": name, "key_id": profile.key_id},
                    )
                state = self.store.load_runtime(name)
                stopped_owned_process = False
                removed_stale_runtime = False
                if state is not None:
                    if state.has_remote_identity:
                        stopped_owned_process = self._stop_owned_and_confirm_remote_release(profile, state)
                    else:
                        result = self.tunnel.disconnect(name)
                        if result.passed:
                            stopped_owned_process = "stopped owned SSH process" in result.detail
                        elif result.error_code == "PROCESS_IDENTITY_MISMATCH":
                            # Legacy runtime has no remote ownership evidence to retain.
                            # TunnelManager did not signal the mismatched local PID.
                            self.store.clear_runtime(name)
                            removed_stale_runtime = True
                        else:
                            raise RABError(
                                "PROFILE_DELETE_TUNNEL_STOP_FAILED",
                                result.detail,
                                retryable=True,
                                details={"name": name, "tunnel_error": result.error_code},
                            )

                remaining = self.store.load_runtime(name)
                if remaining is not None:
                    raise RABError(
                        "PROFILE_DELETE_RUNTIME_REMAINS",
                        "profile runtime still exists after the safe disconnect attempt",
                        retryable=True,
                        details={"name": name},
                    )
                self.store.delete_profile(name)
                return ProfileDeleteResult(name, stopped_owned_process, removed_stale_runtime)
        except ProfileLockError as exc:
            raise self._busy(name, exc) from exc
        except ProfileNotFoundError as exc:
            raise RABError("PROFILE_NOT_FOUND", str(exc), details={"name": name}) from exc

    def _stop_owned_and_confirm_remote_release(self, profile: Profile, state: RuntimeState) -> bool:
        if not self.tunnel.inspector.matches(state):
            raise self._remote_state_unresolved(profile.name)

        stopped = self.tunnel.inspector.terminate_owned(state, self.tunnel.stop_timeout)
        if not stopped:
            if not self.tunnel.inspector.matches(state):
                raise self._remote_state_unresolved(profile.name)
            raise RABError(
                "PROFILE_DELETE_TUNNEL_STOP_FAILED",
                "owned SSH process did not exit before timeout",
                retryable=True,
                details={"name": profile.name},
            )

        try:
            listener = self.tunnel.remote_probe.check_listener(profile)
        except Exception as exc:
            raise self._remote_state_unresolved(profile.name, "REMOTE_CHECK_EXCEPTION", str(exc)) from exc

        if listener.error_code != "LISTENER_ABSENT":
            remote_check = listener.error_code or "LISTENER_PRESENT"
            raise self._remote_state_unresolved(profile.name, remote_check, listener.detail)

        self.store.clear_runtime(profile.name)
        return True

    @staticmethod
    def _busy(name: str, cause: Exception) -> RABError:
        return RABError(
            "PROFILE_BUSY",
            f"profile '{name}' is currently used by an active supervisor",
            retryable=True,
            details={"name": name, "reason": str(cause)},
        )

    @staticmethod
    def _remote_state_unresolved(
        name: str,
        remote_check: str | None = None,
        detail: str | None = None,
    ) -> RABError:
        details = {"name": name}
        if remote_check is not None:
            details["remote_check"] = remote_check
        if detail is not None:
            details["detail"] = detail
        return RABError(
            "PROFILE_DELETE_REMOTE_STATE_UNRESOLVED",
            "profile retains remote tunnel ownership metadata; complete safe cleanup before deleting it",
            retryable=True,
            details=details,
        )
