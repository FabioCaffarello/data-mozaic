"""Least-privilege ports for the isolated Scout discovery-tool adapter."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..domain import SourceRef
from .policy import EffectiveScope


@dataclass(frozen=True)
class TrustedToolContext:
    principal: str
    session_id: str
    call_id: str


@dataclass(frozen=True)
class BudgetState:
    started_at: datetime
    consumed_requests: int
    scope: EffectiveScope


@dataclass(frozen=True)
class EvidenceReference:
    evidence_id: str
    sha256: str
    size_bytes: int
    source: SourceRef


class ToolContextPort(Protocol):
    """The host derives identity/session/call IDs; agents cannot submit them."""

    def current(self) -> TrustedToolContext: ...


class ClockPort(Protocol):
    def now(self) -> datetime: ...


class AuthorizationPort(Protocol):
    def authorize(
        self, principal: str, source: SourceRef, now: datetime
    ) -> EffectiveScope: ...


class BudgetLedgerPort(Protocol):
    """A durable BC14 implementation must atomically consume each HTTP hop."""

    def consume(
        self,
        session_id: str,
        call_id: str,
        hop: int,
        scope: EffectiveScope,
    ) -> BudgetState: ...


class EvidenceStorePort(Protocol):
    """BC06 owns the raw response bytes and returns a durable reference."""

    def store(
        self,
        session_id: str,
        source: SourceRef,
        url: str,
        status: int,
        content_type: str,
        body: bytes,
        now: datetime,
    ) -> EvidenceReference: ...


class TracePort(Protocol):
    def append(
        self, session_id: str, event_type: str, fields: dict, now: datetime
    ) -> None: ...
