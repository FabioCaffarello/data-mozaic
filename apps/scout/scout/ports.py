"""Only ports consumed by the offline use case."""

from datetime import datetime
from typing import Protocol

from .domain import (
    ArtifactRef,
    PolicyDecisionRef,
    ScoutScope,
    ScoutSession,
    SourceRef,
)

CUSTODY_STATUS_AVAILABLE = "available"


class SourceLookupPort(Protocol):
    def resolve(self, source: SourceRef) -> ScoutScope: ...


class PolicyDecisionPort(Protocol):
    def decide(
        self, principal: str, source: SourceRef, now: datetime
    ) -> PolicyDecisionRef: ...


class ScoutSessionRepositoryPort(Protocol):
    def find_intent_header(
        self, principal: str, key: str, digest: str
    ) -> dict | None: ...

    def find_intent(
        self, principal: str, key: str, digest: str
    ) -> ScoutSession | None: ...
    def create(self, session: ScoutSession, key: str, digest: str) -> None: ...
    def save(self, session: ScoutSession, expected_version: int) -> None: ...
    def handoff_status(self, event_id: str) -> dict | None: ...
    def begin_custody_intent(
        self,
        artifact_id: str,
        session_id: str,
        sha256: str,
        size_bytes: int | None = None,
    ) -> None: ...
    def mark_custody_unknown(self, artifact_id: str) -> None: ...
    def confirm_custody_intent(
        self, artifact_id: str, sha256: str, artifact_ref: dict
    ) -> None: ...
    def get_custody_intent(self, artifact_id: str) -> dict | None: ...
    def mark_custody_discrepancy(self, artifact_id: str, note: str) -> None: ...
    def record_orphan_review(
        self, review_id: str, artifact_id: str, diagnostic: dict
    ) -> None: ...


class EvidenceCustodyPort(Protocol):
    """BC06 adapter returns metadata only; bytes remain owned by BC06.

    `lookup` returns an ArtifactRef with size and a normalized custody_status.
    The application accepts only `available`; adapters map their own lifecycle
    vocabulary to this small collaboration result.
    """

    def lookup(self, artifact_id: str) -> ArtifactRef | None: ...

    def store(
        self, session_id: str, source: SourceRef, content: bytes, artifact_id: str
    ) -> ArtifactRef: ...
    def verify(self, artifact: ArtifactRef) -> bool: ...


class ProposalHandoffPort(Protocol):
    def deliver_message(self, message_id: str, payload: dict) -> dict: ...
