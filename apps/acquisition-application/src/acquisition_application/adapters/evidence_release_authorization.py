"""Small operator allowlist adapter for controlled evidence release."""

from __future__ import annotations

from collections.abc import Iterable


class EvidenceReleaseOperatorAllowlist:
    """Authorize named operators configured by the trusted composition root."""

    def __init__(self, operator_ids: Iterable[str]) -> None:
        self._operator_ids = frozenset(
            value.strip() for value in operator_ids if value.strip()
        )

    def assert_can_release(
        self, operator_identity: str, tenant_id: str, run_id: str
    ) -> None:
        if (
            not operator_identity
            or operator_identity not in self._operator_ids
            or not tenant_id
            or not run_id
        ):
            raise PermissionError("operator is not authorized for evidence release")
