"""BC13 logical input and immutable submission intent."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import rfc8785
from mozaic_contract_resolution import ContractReference

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_MAX_PARAMETERS = 64_000


class WorkIntakeError(ValueError):
    """A logical input or submission violates BC13 invariants."""


class DispatchRejected(WorkIntakeError):
    """A definitive release or BC04 admission rejection that needs no retry."""

    def __init__(self, diagnostic_code: str):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", diagnostic_code):
            raise WorkIntakeError("dispatch diagnostic code is invalid")
        self.diagnostic_code = diagnostic_code
        super().__init__(diagnostic_code)


class DispatchState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise WorkIntakeError(f"{label} is invalid")
    return value


def canonical_parameters(value: object) -> tuple[bytes, str]:
    if not isinstance(value, dict) or len(value) > 128:
        raise WorkIntakeError("input parameters must be a bounded object")
    try:
        encoded = rfc8785.dumps(value)
    except Exception as error:
        raise WorkIntakeError("input parameters are not canonical JSON") from error
    if len(encoded) > _MAX_PARAMETERS:
        raise WorkIntakeError("input parameters exceed the size limit")
    return encoded, "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class LogicalInput:
    tenant_id: str
    input_id: str
    source_id: str
    source_revision_id: str
    contract: ContractReference

    def __post_init__(self) -> None:
        for value, label in (
            (self.tenant_id, "tenant identity"),
            (self.input_id, "input identity"),
            (self.source_id, "source identity"),
            (self.source_revision_id, "source revision identity"),
        ):
            _identifier(value, label)


@dataclass(frozen=True, slots=True)
class SubmissionIntent:
    tenant_id: str
    submission_id: str
    input_id: str
    source_id: str
    source_revision_id: str
    idempotency_scope: str
    idempotency_key: str
    release_id: str
    input_contract: ContractReference
    parameters_json: bytes
    parameters_digest: str
    created_at: datetime
    dispatch_operation_id: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.tenant_id, "tenant identity"),
            (self.submission_id, "submission identity"),
            (self.input_id, "input identity"),
            (self.source_id, "source identity"),
            (self.source_revision_id, "source revision identity"),
            (self.idempotency_scope, "idempotency scope"),
            (self.idempotency_key, "idempotency key"),
            (self.release_id, "release identity"),
            (self.dispatch_operation_id, "dispatch operation identity"),
        ):
            _identifier(value, label)
        try:
            UUID(self.submission_id)
        except (ValueError, AttributeError) as error:
            raise WorkIntakeError("submission identity must be a UUID") from error
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise WorkIntakeError("submission timestamp must include a timezone")
        if len(self.parameters_json) > _MAX_PARAMETERS:
            raise WorkIntakeError("input parameters exceed the size limit")
        if (
            "sha256:" + hashlib.sha256(self.parameters_json).hexdigest()
            != self.parameters_digest
        ):
            raise WorkIntakeError("input parameter digest does not match bytes")
        try:
            parameters = json.loads(
                self.parameters_json.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite JSON number")
                ),
            )
            if (
                not isinstance(parameters, dict)
                or rfc8785.dumps(parameters) != self.parameters_json
            ):
                raise ValueError("parameter bytes are not canonical JSON")
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError) as error:
            raise WorkIntakeError("input parameter bytes are invalid") from error

    def intent_digest(self) -> str:
        document: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "input_id": self.input_id,
            "source_id": self.source_id,
            "source_revision_id": self.source_revision_id,
            "idempotency_scope": self.idempotency_scope,
            "idempotency_key": self.idempotency_key,
            "release_id": self.release_id,
            "dispatch_operation_id": self.dispatch_operation_id,
            "input_contract": {
                "contract_id": self.input_contract.contract_id,
                "version": self.input_contract.version,
                "digest": self.input_contract.digest,
            },
            "parameters_digest": self.parameters_digest,
        }
        return "sha256:" + hashlib.sha256(rfc8785.dumps(document)).hexdigest()


@dataclass(frozen=True, slots=True)
class Submission:
    intent: SubmissionIntent
    state: DispatchState
    run_id: str | None = None
    diagnostic_code: str | None = None
    claim_owner: str | None = None
    claim_generation: int = 0


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result
