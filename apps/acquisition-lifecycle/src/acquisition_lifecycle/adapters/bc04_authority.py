"""BC04 public snapshot registry and verifier adapter for the composition root."""

from __future__ import annotations

from collections.abc import Mapping

from execution_coordination.adapters.trusted_live_authority import TrustedLiveAuthority
from execution_coordination.durable_ports import AuthoritySnapshotRegistryPort


class BC04AuthoritySnapshotAdapter:
    def __init__(self, registry: AuthoritySnapshotRegistryPort):
        self._registry = registry

    def current_generation(self, authority_id: str) -> int:
        head = self._registry.authority_snapshot_head(authority_id)
        return int(head[0]) if head is not None else 0

    def current_snapshot(self, authority_id: str) -> tuple[int, str] | None:
        head = self._registry.authority_snapshot_head(authority_id)
        return (int(head[0]), str(head[1])) if head is not None else None

    def install(
        self, envelope: bytes, trusted_public_keys: Mapping[str, bytes]
    ) -> TrustedLiveAuthority:
        return TrustedLiveAuthority(
            envelope,
            trusted_public_keys=trusted_public_keys,
            minimum_generation=1,
            authority_registry=self._registry,
        )
