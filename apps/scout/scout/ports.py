"""Only ports consumed by the offline use case."""

from dataclasses import dataclass
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


@dataclass(frozen=True)
class FixtureLink:
    url: str
    origin: str
    path: str


@dataclass(frozen=True)
class FixtureExplorationResult:
    """Untrusted bytes and normalized metadata returned by a hermetic adapter."""

    fixture_id: str
    url: str
    origin: str
    path: str
    status: int
    content_type: str
    body: bytes
    title: str | None
    text: str
    links: tuple[FixtureLink, ...]
    elapsed_seconds: int = 1


class FixtureExplorationPort(Protocol):
    def explore(
        self, source: SourceRef, scope: ScoutScope, fixture_id: str, max_bytes: int
    ) -> FixtureExplorationResult: ...


class ScoutSessionRepositoryPort(Protocol):
    def get_session_header(self, session_id: str) -> dict | None: ...
    def get_session(self, session_id: str) -> ScoutSession | None: ...
    def find_command_header(
        self, principal: str, key: str, fingerprint: str
    ) -> dict | None: ...
    def begin_command(
        self,
        principal: str,
        session_id: str,
        key: str,
        command_id: str,
        command_kind: str,
        fingerprint: str,
        intent: dict,
    ) -> dict: ...
    def get_command_result(
        self, principal: str, key: str, fingerprint: str
    ) -> ScoutSession | None: ...
    def has_incomplete_commands(self, session_id: str) -> bool: ...
    def complete_command(
        self,
        session: ScoutSession,
        expected_version: int,
        principal: str,
        key: str,
        fingerprint: str,
    ) -> None: ...
    def mark_command_uncertain(self, principal: str, key: str) -> None: ...
    def find_intent_header(
        self, principal: str, key: str, digest: str
    ) -> dict | None: ...

    def find_intent(
        self, principal: str, key: str, digest: str
    ) -> ScoutSession | None: ...
    def create(
        self,
        session: ScoutSession,
        key: str,
        digest: str,
        *,
        command_kind: str = "RunScout",
        receipt_status: str = "in_progress",
    ) -> None: ...
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
