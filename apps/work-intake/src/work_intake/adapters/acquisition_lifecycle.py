"""Release-resolution adapter over the M4 operator composition boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from acquisition_lifecycle.application import AcquisitionLifecycle
from acquisition_lifecycle.domain import LifecycleError

from work_intake.domain import DispatchRejected


class AcquisitionLifecycleReleaseResolver:
    """Project an eligible approved BotRelease document into BC13's port."""

    def __init__(self, lifecycle: AcquisitionLifecycle) -> None:
        self._lifecycle = lifecycle

    def resolve_eligible_release(self, release_id: str) -> Mapping[str, Any]:
        try:
            result = self._lifecycle.resolve_eligible_release(release_id)
        except LifecycleError as error:
            try:
                historical = self._lifecycle.inspect_release(release_id)
            except LifecycleError as unresolved:
                raise DispatchRejected("unknown_release") from unresolved
            if historical.get("state") == "revoked":
                raise DispatchRejected("release_revoked") from error
            raise
        document = result.get("approved_release")
        if (
            not isinstance(document, Mapping)
            or document.get("release_id") != release_id
        ):
            raise LifecycleError("eligible release projection is invalid")
        return document
