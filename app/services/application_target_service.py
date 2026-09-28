from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from app.domain.application_target import (
    ApplicationTarget,
    ApplicationTargetValidationError,
)
from app.domain.errors import RABError
from app.infrastructure.application_target_store import (
    ApplicationTargetNotFoundError,
    ApplicationTargetStore,
)
from app.services.profile_service import ProfileService


UPDATABLE_APPLICATION_TARGET_FIELDS = frozenset(
    {"profile_name", "service_unit", "health_url"}
)


class ApplicationTargetService:
    def __init__(
        self,
        store: ApplicationTargetStore,
        profiles: ProfileService,
    ) -> None:
        self.store = store
        self.profiles = profiles

    def create(self, target: ApplicationTarget) -> ApplicationTarget:
        try:
            target.validate()
            self._verify_profile(target)
            self.store.save_target(target)
            return target
        except FileExistsError as exc:
            raise RABError(
                "APPLICATION_TARGET_EXISTS",
                str(exc),
                details={"name": target.name},
            ) from exc
        except ApplicationTargetValidationError as exc:
            raise self._invalid(target.name, exc) from exc

    def get(self, name: str) -> ApplicationTarget:
        try:
            return self.store.load_target(name)
        except ApplicationTargetNotFoundError as exc:
            raise RABError(
                "APPLICATION_TARGET_NOT_FOUND",
                str(exc),
                details={"name": name},
            ) from exc
        except ApplicationTargetValidationError as exc:
            raise self._invalid(name, exc) from exc

    def list(self) -> list[ApplicationTarget]:
        try:
            return self.store.list_targets()
        except ApplicationTargetValidationError as exc:
            raise self._invalid("persisted", exc) from exc

    def update(self, name: str, changes: Mapping[str, Any]) -> ApplicationTarget:
        if not changes:
            raise RABError(
                "APPLICATION_TARGET_UPDATE_EMPTY",
                "no application target fields were provided",
                details={"name": name},
            )
        invalid_fields = sorted(set(changes) - UPDATABLE_APPLICATION_TARGET_FIELDS)
        if invalid_fields:
            raise RABError(
                "APPLICATION_TARGET_FIELD_NOT_UPDATABLE",
                f"application target fields cannot be updated: {', '.join(invalid_fields)}",
                details={"name": name, "fields": invalid_fields},
            )
        try:
            current = self.store.load_target(name)
            updated = replace(current, **dict(changes))
            updated.validate()
            self._verify_profile(updated)
            self.store.update_target(updated)
            return updated
        except ApplicationTargetNotFoundError as exc:
            raise RABError(
                "APPLICATION_TARGET_NOT_FOUND",
                str(exc),
                details={"name": name},
            ) from exc
        except ApplicationTargetValidationError as exc:
            raise self._invalid(name, exc) from exc

    def delete(self, name: str) -> None:
        try:
            self.store.delete_target(name)
        except ApplicationTargetNotFoundError as exc:
            raise RABError(
                "APPLICATION_TARGET_NOT_FOUND",
                str(exc),
                details={"name": name},
            ) from exc

    def _verify_profile(self, target: ApplicationTarget) -> None:
        try:
            self.profiles.get(target.profile_name)
        except RABError as exc:
            if exc.code != "PROFILE_NOT_FOUND":
                raise
            raise RABError(
                "APPLICATION_TARGET_PROFILE_NOT_FOUND",
                "application target profile does not exist",
                details={"name": target.name, "profile_name": target.profile_name},
            ) from exc

    @staticmethod
    def _invalid(name: str, cause: Exception) -> RABError:
        return RABError(
            "APPLICATION_TARGET_INVALID",
            str(cause),
            details={"name": name},
        )
