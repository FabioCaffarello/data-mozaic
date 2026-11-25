"""Durable BC04 run lifecycle and finalization invariants.

The original ``CaptureRun`` model remains the in-process fixture coordinator
aggregate. This model adds the history, leases, generations, and finalization
reservation required when multiple coordinator processes share PostgreSQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from execution_coordination.domain import CoordinationError, ExecutionBinding

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_MAX = 256
_MAX_ATTEMPTS = 5


def _identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > _ID_MAX:
        raise CoordinationError(f"{label} must be a non-empty bounded string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CoordinationError(f"{label} contains invalid Unicode") from error


class DurableRunState(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    FINALIZATION_PENDING = "finalization_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class DurableAttemptState(StrEnum):
    LEASED = "leased"
    RUNNING = "running"
    RETRYABLE_FAILURE = "retryable_failure"
    FAILED = "failed"
    EXPIRED = "expired"
    FINALIZATION_PENDING = "finalization_pending"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"


class FailureDisposition(StrEnum):
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DurableRunIntent:
    tenant_id: str
    run_id: str
    request_id: str
    idempotency_key: str
    binding: ExecutionBinding
    deadline: datetime
    max_attempts: int = 3

    def __post_init__(self) -> None:
        for value, label in (
            (self.tenant_id, "tenant_id"),
            (self.request_id, "request_id"),
            (self.idempotency_key, "idempotency_key"),
        ):
            _identity(value, label)
        try:
            UUID(self.run_id)
        except (ValueError, AttributeError) as error:
            raise CoordinationError("run_id must be a UUID") from error
        if self.deadline.tzinfo is None or self.deadline.utcoffset() is None:
            raise CoordinationError("run deadline must be timezone-aware")
        if not 1 <= self.max_attempts <= _MAX_ATTEMPTS:
            raise CoordinationError("max_attempts is outside the supported budget")


@dataclass(frozen=True, slots=True)
class AttemptLease:
    tenant_id: str
    run_id: str
    attempt_id: str
    generation: int
    lease_owner: str
    lease_token: str
    lease_until: datetime
    attempt_deadline: datetime

    def __post_init__(self) -> None:
        for value, label in (
            (self.tenant_id, "tenant_id"),
            (self.lease_owner, "lease_owner"),
            (self.lease_token, "lease_token"),
        ):
            _identity(value, label)
        for value, label in ((self.run_id, "run_id"), (self.attempt_id, "attempt_id")):
            try:
                UUID(value)
            except (ValueError, AttributeError) as error:
                raise CoordinationError(f"{label} must be a UUID") from error
        if self.generation < 1:
            raise CoordinationError("fencing generation must be positive")
        for value in (self.lease_until, self.attempt_deadline):
            if value.tzinfo is None or value.utcoffset() is None:
                raise CoordinationError("lease timestamps must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FinalizationGrant:
    contract_version: str
    issuer: str
    recipient: str
    algorithm: str
    key_id: str
    tenant_id: str
    run_id: str
    attempt_id: str
    fencing_generation: int
    output_set_id: str
    manifest_digest: str
    reservation_id: str
    operation_id: str
    issued_at: datetime
    signature: str

    def __post_init__(self) -> None:
        if self.contract_version != "publication-grant/v1":
            raise CoordinationError("unsupported publication grant contract")
        for value, label in (
            (self.issuer, "grant issuer"),
            (self.recipient, "grant recipient"),
            (self.key_id, "grant key id"),
            (self.tenant_id, "grant tenant"),
            (self.output_set_id, "grant OutputSet"),
            (self.reservation_id, "reservation id"),
            (self.operation_id, "grant operation id"),
        ):
            _identity(value, label)
        for value, label in ((self.run_id, "run_id"), (self.attempt_id, "attempt_id")):
            try:
                UUID(value)
            except (ValueError, AttributeError) as error:
                raise CoordinationError(f"{label} must be a UUID") from error
        if self.fencing_generation < 1:
            raise CoordinationError("grant fencing generation must be positive")
        if not _DIGEST.fullmatch(self.manifest_digest):
            raise CoordinationError("grant manifest digest is invalid")
        if self.issued_at.tzinfo is None or self.issued_at.utcoffset() is None:
            raise CoordinationError("grant issued_at must be timezone-aware")
        if self.algorithm != "Ed25519":
            raise CoordinationError("grant signature algorithm is unsupported")
        if not re.fullmatch(r"ed25519:[A-Za-z0-9_-]{86}", self.signature):
            raise CoordinationError("grant signature has invalid encoding")

    def fields(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "issuer": self.issuer,
            "recipient": self.recipient,
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "tenant_id": self.tenant_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "fencing_generation": self.fencing_generation,
            "output_set_id": self.output_set_id,
            "manifest_digest": self.manifest_digest,
            "reservation_id": self.reservation_id,
            "operation_id": self.operation_id,
            "issued_at": self.issued_at.astimezone(UTC).isoformat(),
            "signature": self.signature,
        }


@dataclass(frozen=True, slots=True)
class FinalizationReservation:
    reservation_id: str
    tenant_id: str
    run_id: str
    attempt_id: str
    fencing_generation: int
    output_set_id: str
    manifest_digest: str
    operation_id: str
    grant: FinalizationGrant
    created_at: datetime

    def __post_init__(self) -> None:
        if not _DIGEST.fullmatch(self.manifest_digest):
            raise CoordinationError("reservation manifest digest is invalid")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise CoordinationError("reservation created_at must be timezone-aware")
        expected = (
            self.tenant_id,
            self.run_id,
            self.attempt_id,
            self.fencing_generation,
            self.output_set_id,
            self.manifest_digest,
            self.reservation_id,
            self.operation_id,
        )
        actual = (
            self.grant.tenant_id,
            self.grant.run_id,
            self.grant.attempt_id,
            self.grant.fencing_generation,
            self.grant.output_set_id,
            self.grant.manifest_digest,
            self.grant.reservation_id,
            self.grant.operation_id,
        )
        if expected != actual:
            raise CoordinationError("reservation and signed grant differ")


@dataclass(frozen=True, slots=True)
class DurableRunSnapshot:
    intent: DurableRunIntent
    state: DurableRunState
    generation: int
    attempt_count: int
    attempt_state: DurableAttemptState | None
    lease_until: datetime | None
    reservation: FinalizationReservation | None
    publication_receipt: dict[str, object] | None
    cancel_requested_at: datetime | None
    terminal_reason: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None

    @property
    def attempt_id(self) -> str | None:
        if self.generation == 0:
            return None
        from uuid import NAMESPACE_URL, uuid5

        return str(
            uuid5(
                NAMESPACE_URL,
                f"data-mozaic:bc04:{self.intent.run_id}:{self.generation}",
            )
        )
