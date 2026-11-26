"""Consumer-side ports for lifecycle persistence and operator trust."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol


class LifecycleStore(Protocol):
    def migrate(self) -> None: ...

    def register_source(self, document: dict[str, Any], actor_id: str) -> str: ...

    def resolve_source(self, source_id: str, revision_id: str) -> dict[str, Any]: ...

    def set_source_state(
        self, source_id: str, revision_id: str, state: str, actor_id: str
    ) -> None: ...

    def list_sources(self) -> list[dict[str, Any]]: ...

    def register_policy(self, document: dict[str, Any], actor_id: str) -> str: ...

    def resolve_policy(self, policy_id: str, version: str) -> dict[str, Any]: ...

    def set_policy_state(
        self, policy_id: str, version: str, state: str, actor_id: str
    ) -> None: ...

    def list_policies(self) -> list[dict[str, Any]]: ...

    def register_playbook(
        self,
        playbook_id: str,
        digest: str,
        source_id: str,
        revision_id: str,
        content: bytes,
        actor_id: str,
    ) -> None: ...

    def resolve_playbook(self, playbook_id: str, digest: str) -> dict[str, Any]: ...

    def register_build(
        self,
        build_id: str,
        manifest_digest: str,
        runtime_digest: str,
        path: Path,
        manifest: dict[str, Any],
        actor_id: str,
    ) -> None: ...

    def resolve_build(
        self, build_id: str, runtime_image_digest: str
    ) -> dict[str, Any]: ...

    def create_release(
        self, release_id: str, candidate: dict[str, Any], digest: str, actor_id: str
    ) -> None: ...

    def approve_release(
        self,
        release_id: str,
        document: dict[str, Any],
        actor_id: str,
        evidence_ref: str,
    ) -> None: ...

    def revoke_release(
        self, release_id: str, actor_id: str, evidence_ref: str
    ) -> None: ...

    def resolve_release(
        self, release_id: str, *, require_eligible: bool = True
    ) -> dict[str, Any]: ...

    def list_releases(self) -> list[dict[str, Any]]: ...

    def prepare_authority(
        self,
        authority_id: str,
        generation: int,
        snapshot_digest: str,
        envelope_digest: str,
        envelope: dict[str, Any],
    ) -> dict[str, Any]: ...

    def pending_authority(self, authority_id: str) -> dict[str, Any] | None: ...

    def latest_authority_generation(self, authority_id: str) -> int: ...

    def supersede_expired_authority(
        self, authority_id: str, generation: int, envelope_digest: str
    ) -> None: ...

    def mark_authority_installed(self, authority_id: str, generation: int) -> None: ...


class OperatorApprovalPort(Protocol):
    """Trusted local operator boundary; not a general identity/RBAC service."""

    def assert_can_approve(self, actor_id: str, release_id: str) -> str: ...


class BotBuildVerifierPort(Protocol):
    def verify(
        self, artifact_path: Path, expected_manifest_digest: str
    ) -> dict[str, Any]: ...


class AuthoritySignerPort(Protocol):
    key_id: str

    def sign(self, canonical_snapshot: bytes) -> str: ...


class AuthoritySnapshotPort(Protocol):
    def current_generation(self, authority_id: str) -> int: ...

    def current_snapshot(self, authority_id: str) -> tuple[int, str] | None: ...

    def install(
        self, envelope: bytes, trusted_public_keys: Mapping[str, bytes]
    ) -> Any: ...
