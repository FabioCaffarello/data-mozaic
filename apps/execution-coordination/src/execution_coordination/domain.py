"""Engine-independent CaptureRun and CaptureAttempt domain model (BC04)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class State(StrEnum):
    ACCEPTED = "accepted"
    LAUNCHING = "launching"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TERMINAL = {
    State.SUCCEEDED,
    State.FAILED,
    State.TIMED_OUT,
    State.CANCELLED,
}


class CoordinationError(ValueError):
    """A requested state transition or identity is invalid."""


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ExecutionBinding:
    source_id: str
    source_revision: str
    bot_id: str
    build_id: str
    bot_build_manifest_digest: str
    assembly_id: str
    catalog_digest: str
    oci_image: str
    oci_digest: str
    local_image_id: str
    fixture_bundle_id: str
    fixture_bundle_digest: str
    payload_contract_id: str | None = None
    payload_contract_version: str | None = None
    payload_contract_digest: str | None = None
    execution_mode: str = "fixture-only"
    max_duration_seconds: int = 30
    max_output_bytes: int = 16_777_216
    max_records: int = 10_000

    def __post_init__(self) -> None:
        values = (
            self.source_id,
            self.source_revision,
            self.bot_id,
            self.build_id,
            self.bot_build_manifest_digest,
            self.assembly_id,
            self.catalog_digest,
            self.oci_image,
            self.oci_digest,
            self.local_image_id,
            self.fixture_bundle_id,
            self.fixture_bundle_digest,
        )
        if any(not value for value in values):
            raise CoordinationError("execution binding identities must be non-empty")
        contract_values = (
            self.payload_contract_id,
            self.payload_contract_version,
            self.payload_contract_digest,
        )
        if any(value is not None for value in contract_values):
            if any(
                not isinstance(value, str) or not value for value in contract_values
            ):
                raise CoordinationError("payload contract binding is incomplete")
            if not re.fullmatch(
                r"[a-z0-9][a-z0-9._:-]{0,127}", self.payload_contract_id or ""
            ):
                raise CoordinationError("payload contract identity is invalid")
            if not re.fullmatch(
                r"[a-z0-9][a-z0-9._:-]{0,127}", self.payload_contract_version or ""
            ):
                raise CoordinationError("payload contract version is invalid")
            if not re.fullmatch(
                r"sha256:[0-9a-f]{64}", self.payload_contract_digest or ""
            ):
                raise CoordinationError("payload contract digest is invalid")
        for digest in (
            self.build_id,
            self.bot_build_manifest_digest,
            self.assembly_id,
            self.catalog_digest,
            self.fixture_bundle_digest,
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise CoordinationError("execution binding contains an invalid digest")
        if not re.fullmatch(r"bot-[0-9a-f]{64}", self.bot_id):
            raise CoordinationError("bot identity is invalid")
        if not re.fullmatch(
            r"sha256:[0-9a-f]{64}", self.oci_digest
        ) or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.local_image_id):
            raise CoordinationError(
                "worker image identities must be explicit SHA-256 values"
            )
        if not self.oci_image.endswith("@" + self.oci_digest):
            raise CoordinationError("OCI reference and OCI digest differ")
        if self.execution_mode != "fixture-only":
            raise CoordinationError("only fixture-only execution is supported")
        if self.max_duration_seconds <= 0 or self.max_duration_seconds > 300:
            raise CoordinationError("duration budget is outside trusted limits")
        if (
            self.max_output_bytes <= 0
            or self.max_output_bytes > 16_777_216
            or self.max_records <= 0
            or self.max_records > 10_000
        ):
            raise CoordinationError("output budgets exceed the fixture profile maximum")


def request_identities(binding: ExecutionBinding) -> dict[str, str]:
    """Project the immutable BC04 binding into the worker report contract."""
    return {
        "source_id": binding.source_id,
        "source_revision": binding.source_revision,
        "bot_id": binding.bot_id,
        "build_id": binding.build_id,
        "bot_build_manifest_digest": binding.bot_build_manifest_digest,
        "assembly_id": binding.assembly_id,
        "catalog_digest": binding.catalog_digest,
        "oci_digest": binding.oci_digest,
        "local_image_id": binding.local_image_id,
        "fixture_bundle_id": binding.fixture_bundle_id,
        "fixture_bundle_digest": binding.fixture_bundle_digest,
    }


@dataclass(frozen=True)
class ExecutionRequest:
    contract_version: str
    request_id: str
    run_id: str
    attempt_id: str
    binding: ExecutionBinding
    deadline: datetime
    execution_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.contract_version != "execution-request/v0":
            raise CoordinationError("unsupported execution request contract")
        if not self.request_id or len(self.request_id) > 128:
            raise CoordinationError("request identity is required and bounded")
        try:
            uuid.UUID(self.run_id)
            uuid.UUID(self.attempt_id)
            uuid.UUID(self.execution_id)
        except (ValueError, AttributeError) as error:
            raise CoordinationError(
                "run and attempt identities must be UUIDs"
            ) from error
        if self.deadline.tzinfo is None:
            raise CoordinationError("deadline must be timezone-aware")
        allowed_correlation = {"trace_id", "correlation_id", "intake_id"}
        if len(self.correlation) > 8 or any(
            k not in allowed_correlation or not isinstance(v, str) or len(v) > 128
            for k, v in self.correlation.items()
        ):
            raise CoordinationError("correlation metadata exceeds the allowlist")


@dataclass(frozen=True)
class ExecutionReport:
    contract_version: str
    request_id: str
    run_id: str
    attempt_id: str
    execution_id: str
    technical_status: str
    binding: ExecutionBinding
    duration_ms: int
    records_count: int = 0
    output_bytes: int = 0
    output_sha256: str | None = None
    output_ref: str | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.contract_version != "execution-report/v0":
            raise CoordinationError("unsupported execution report contract")
        if self.technical_status not in {
            "succeeded",
            "failed",
            "timed_out",
            "cancelled",
            "crashed",
        }:
            raise CoordinationError("unknown technical status")
        if self.duration_ms < 0 or self.records_count < 0 or self.output_bytes < 0:
            raise CoordinationError("report counters cannot be negative")
        try:
            uuid.UUID(self.run_id)
            uuid.UUID(self.attempt_id)
            uuid.UUID(self.execution_id)
        except (ValueError, AttributeError) as error:
            raise CoordinationError("report identities must be UUIDs") from error
        if self.technical_status == "succeeded" and (
            not self.output_ref
            or not re.fullmatch(r"[0-9a-f]{64}", self.output_sha256 or "")
        ):
            raise CoordinationError("successful reports require verified staged output")
        if self.technical_status != "succeeded" and self.output_ref is not None:
            raise CoordinationError("non-success reports cannot expose output")
        if self.technical_status != "succeeded" and (
            self.records_count or self.output_bytes or self.output_sha256
        ):
            raise CoordinationError("non-success reports cannot contain partial output")


@dataclass
class CaptureAttempt:
    attempt_id: str
    execution_id: str | None = None
    state: State = State.ACCEPTED
    report: ExecutionReport | None = None
    diagnostic: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None

    def set_state(self, state: State, *, at: datetime | None = None) -> None:
        if state == State.SUCCEEDED:
            raise CoordinationError("Attempt success requires a verified report")
        self._apply_state(state, at=at)

    def _apply_state(self, state: State, *, at: datetime | None = None) -> None:
        allowed = {
            State.ACCEPTED: {
                State.LAUNCHING,
                State.CANCEL_REQUESTED,
                State.FAILED,
                State.TIMED_OUT,
                State.CANCELLED,
            },
            State.LAUNCHING: {
                State.RUNNING,
                State.CANCEL_REQUESTED,
                State.FAILED,
                State.TIMED_OUT,
                State.CANCELLED,
            },
            State.RUNNING: TERMINAL | {State.CANCEL_REQUESTED},
            State.CANCEL_REQUESTED: {
                State.CANCELLED,
                State.TIMED_OUT,
                State.FAILED,
            },
        }
        if state not in allowed.get(self.state, set()):
            raise CoordinationError(
                f"invalid Attempt transition {self.state} -> {state}"
            )
        self.state = state
        if state in TERMINAL:
            self.finished_at = at or now_utc()


@dataclass
class CaptureRun:
    run_id: str
    request_id: str
    idempotency_scope: str
    idempotency_key: str
    binding: ExecutionBinding
    deadline: datetime
    state: State = State.ACCEPTED
    attempt: CaptureAttempt | None = None
    terminal_report: ExecutionReport | None = None
    created_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.run_id)
        except (ValueError, AttributeError) as error:
            raise CoordinationError("Run identity must be a UUID") from error
        if (
            not self.request_id
            or not self.idempotency_scope
            or not self.idempotency_key
        ):
            raise CoordinationError(
                "request and scoped idempotency identities are required"
            )
        if self.deadline.tzinfo is None:
            raise CoordinationError("Run deadline must be timezone-aware")
        if len(self.idempotency_scope) > 160 or len(self.idempotency_key) > 160:
            raise CoordinationError("idempotency identity exceeds configured limits")

    def set_state(self, state: State, *, at: datetime | None = None) -> None:
        if state == State.SUCCEEDED:
            raise CoordinationError("Run success requires a verified report")
        self._apply_state(state, at=at)

    def _apply_state(self, state: State, *, at: datetime | None = None) -> None:
        allowed = {
            State.ACCEPTED: {
                State.LAUNCHING,
                State.CANCEL_REQUESTED,
                State.FAILED,
                State.TIMED_OUT,
                State.CANCELLED,
            },
            State.LAUNCHING: {
                State.RUNNING,
                State.CANCEL_REQUESTED,
                State.FAILED,
                State.TIMED_OUT,
                State.CANCELLED,
            },
            State.RUNNING: TERMINAL | {State.CANCEL_REQUESTED},
            State.CANCEL_REQUESTED: {
                State.CANCELLED,
                State.TIMED_OUT,
                State.FAILED,
            },
        }
        if state not in allowed.get(self.state, set()):
            raise CoordinationError(f"invalid Run transition {self.state} -> {state}")
        if self.attempt is not None:
            self.attempt.set_state(state, at=at)
        self.state = state
        if state in TERMINAL:
            self.finished_at = at or now_utc()

    def validate_report(self, report: ExecutionReport) -> None:
        """Validate report identity before output can cross the publication boundary."""
        if (
            self.attempt is None
            or report.run_id != self.run_id
            or report.attempt_id != self.attempt.attempt_id
            or report.request_id != self.request_id
            or report.binding != self.binding
            or report.execution_id != self.attempt.execution_id
        ):
            raise CoordinationError("report identity does not match active Attempt")

    def reconcile(self, report: ExecutionReport, *, at: datetime | None = None) -> bool:
        """Apply the first authoritative terminal fact; exact duplicates are idempotent."""
        if self.state in TERMINAL:
            self.validate_report(report)
            if self.terminal_report == report:
                return False
            raise CoordinationError("conflicting or late report for terminal Run")
        self.validate_report(report)
        if self.state == State.CANCEL_REQUESTED:
            self.set_state(State.CANCELLED, at=at)
            return True
        if (at or now_utc()) >= self.deadline:
            self.set_state(State.TIMED_OUT, at=at)
            return True
        mapped = (
            State.FAILED
            if report.technical_status == "crashed"
            else State(report.technical_status)
        )
        self.attempt.report = report
        self.terminal_report = report
        if report.technical_status == "crashed":
            self.attempt.diagnostic = "WORKER_CRASHED"
        self.attempt._apply_state(mapped, at=at)
        self.state = mapped
        if mapped in TERMINAL:
            self.finished_at = at or now_utc()
        return True
