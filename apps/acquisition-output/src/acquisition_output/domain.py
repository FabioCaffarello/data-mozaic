"""Datastore-independent BC05 output values and OutputSet lifecycle."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar

import rfc8785

MAX_ID_LENGTH = 256
MAX_NATURAL_KEY_LENGTH = 512
MAX_PAYLOAD_BYTES = 1_048_576
MAX_DESCRIPTION_BYTES = 16_384
MAX_ARTIFACTS_PER_OUTPUT = 32
MAX_OUTPUTS_PER_SET = 10_000
MAX_CONTRACTS_PER_SET = 128
MAX_OUTPUT_BYTES_PER_SET = 16_777_216
SAFE_INTEGER = (1 << 53) - 1
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class OutputError(ValueError):
    """Base class for invalid BC05 output state or values."""


class OutputConflict(OutputError):
    """An identity or idempotency key was reused for different intent."""


class InvalidTransition(OutputError):
    """An OutputSet command is not valid in its current state."""


class OutputSetState(StrEnum):
    STAGING = "staging"
    SEALED = "sealed"
    PUBLISHED = "published"
    ABORTED = "aborted"
    REJECTED = "rejected"


def _identifier(value: str, label: str, *, maximum: int = MAX_ID_LENGTH) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise OutputError(f"{label} must be a non-empty bounded string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise OutputError(f"{label} contains invalid Unicode") from error


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise OutputError(f"{label} must be a sha256-prefixed lowercase digest")


def _json_copy(value: object, *, depth: int = 0) -> object:
    """Copy and freeze the RFC 8785/I-JSON subset accepted as payload data."""
    if depth > 64:
        raise OutputError("payload nesting exceeds 64 levels")
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise OutputError("payload contains invalid Unicode") from error
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > SAFE_INTEGER:
            raise OutputError(
                "payload integer exceeds interoperable JSON number range; use a contract-defined string"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OutputError("payload numbers must be finite")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OutputError("payload object keys must be strings")
            _identifier(key, "payload object key", maximum=1024)
            if key in copied:
                raise OutputError("payload object contains duplicate keys")
            copied[key] = _json_copy(item, depth=depth + 1)
        return MappingProxyType(copied)
    if isinstance(value, list):
        return tuple(_json_copy(item, depth=depth + 1) for item in value)
    raise OutputError(f"unsupported payload value type: {type(value).__name__}")


def plain_json(value: object) -> object:
    """Return an RFC 8785 library-compatible copy of frozen JSON data."""
    if isinstance(value, Mapping):
        return {key: plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain_json(item) for item in value]
    return value


def canonical_json(value: object) -> bytes:
    """Serialize supported JSON using RFC 8785; rejects unsupported I-JSON."""
    try:
        return rfc8785.dumps(plain_json(value))
    except Exception as error:
        raise OutputError(
            f"value cannot be canonicalized as RFC 8785 JSON: {type(error).__name__}"
        ) from error


def canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceRevisionRef:
    source_id: str
    revision: str

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _identifier(self.revision, "source revision")


@dataclass(frozen=True, slots=True)
class PayloadContractRef:
    contract_id: str
    version: str
    digest: str

    def __post_init__(self) -> None:
        _identifier(self.contract_id, "payload contract id")
        _identifier(self.version, "payload contract version", maximum=64)
        _digest(self.digest, "payload contract digest")


@dataclass(frozen=True, slots=True)
class ProducerIdentity:
    producer_id: str
    producer_version: str
    capability_id: str | None = None
    capability_version: str | None = None
    release_id: str | None = None
    release_digest: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.producer_id, "producer_id")
        _identifier(self.producer_version, "producer_version", maximum=128)
        if (self.capability_id is None) != (self.capability_version is None):
            raise OutputError(
                "capability_id and capability_version must be supplied together"
            )
        if self.capability_id is not None:
            _identifier(self.capability_id, "capability_id")
            _identifier(
                self.capability_version or "", "capability_version", maximum=128
            )
        if (self.release_id is None) != (self.release_digest is None):
            raise OutputError("release_id and release_digest must be supplied together")
        if self.release_id is not None:
            _identifier(self.release_id, "release_id")
            _digest(self.release_digest or "", "release_digest")


@dataclass(frozen=True, slots=True)
class ExecutionBinding:
    run_id: str | None = None
    attempt_id: str | None = None
    fencing_token: int | None = None

    def __post_init__(self) -> None:
        if (self.run_id is None) != (self.attempt_id is None):
            raise OutputError("run_id and attempt_id must be supplied together")
        if self.run_id is not None:
            _identifier(self.run_id, "run_id")
            _identifier(self.attempt_id or "", "attempt_id")
        if self.fencing_token is not None and (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or not 1 <= self.fencing_token <= SAFE_INTEGER
        ):
            raise OutputError(
                "fencing_token must be a positive interoperable JSON integer"
            )
        if self.fencing_token is not None and self.run_id is None:
            raise OutputError("fencing_token requires a run and attempt binding")


@dataclass(frozen=True, slots=True)
class OutputSetBinding:
    tenant_id: str
    source: SourceRevisionRef
    producer: ProducerIdentity
    execution: ExecutionBinding = field(default_factory=ExecutionBinding)

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "tenant_id")


@dataclass(frozen=True, slots=True)
class OutputIdentity:
    output_id: str
    observation_id: str
    observed_at: datetime
    natural_key: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.output_id, "output_id")
        _identifier(self.observation_id, "observation_id")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise OutputError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        if self.natural_key is not None:
            _identifier(self.natural_key, "natural_key", maximum=MAX_NATURAL_KEY_LENGTH)


def derive_output_id(
    *,
    tenant_id: str,
    source: SourceRevisionRef,
    producer: ProducerIdentity,
    output_key: str,
    observation_id: str,
) -> str:
    """Derive stable observation-scoped identity; payload is deliberately excluded."""
    _identifier(tenant_id, "tenant_id")
    _identifier(output_key, "output_key")
    _identifier(observation_id, "observation_id")
    identity = {
        "identity_version": "capture-output-identity/v1",
        "tenant_id": tenant_id,
        "source": {"source_id": source.source_id, "revision": source.revision},
        "producer": producer_to_wire(producer),
        "output_key": output_key,
        "observation_id": observation_id,
    }
    return "out-" + hashlib.sha256(canonical_json(identity)).hexdigest()


@dataclass(frozen=True, slots=True)
class ScopedOperationIdentity:
    tenant_id: str
    operation_id: str

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "operation tenant")
        _identifier(self.operation_id, "operation_id")


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    digest: str
    size_bytes: int
    media_type: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.artifact_id, "artifact_id")
        _digest(self.digest, "artifact digest")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= SAFE_INTEGER
        ):
            raise OutputError(
                "artifact size_bytes must be a non-negative interoperable JSON integer"
            )
        if self.media_type is not None:
            _identifier(self.media_type, "artifact media_type", maximum=256)


@dataclass(frozen=True, slots=True)
class ArtifactDescription:
    """Display metadata only; filename is a label, never a storage path."""

    file_name: str | None = None
    title: str | None = None
    description: str | None = None
    role: str | None = None

    def __post_init__(self) -> None:
        if all(
            value is None
            for value in (self.file_name, self.title, self.description, self.role)
        ):
            raise OutputError(
                "artifact description must be omitted or contain at least one field"
            )
        for label, value, maximum in (
            ("file_name", self.file_name, 255),
            ("title", self.title, 512),
            ("description", self.description, 2048),
            ("role", self.role, 128),
        ):
            if value is not None:
                _identifier(value, f"artifact {label}", maximum=maximum)
        if self.file_name is not None and (
            "/" in self.file_name or "\\" in self.file_name
        ):
            raise OutputError(
                "artifact file_name is a display name and cannot contain a path"
            )
        canonical_json(self.to_wire())
        if len(canonical_json(self.to_wire())) > MAX_DESCRIPTION_BYTES:
            raise OutputError("artifact description metadata exceeds its byte budget")

    def to_wire(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("file_name", self.file_name),
                ("title", self.title),
                ("description", self.description),
                ("role", self.role),
            )
            if value is not None
        }


@dataclass(frozen=True, slots=True)
class ArtifactItem:
    artifact: ArtifactRef
    description: ArtifactDescription | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CaptureOutput:
    identity: OutputIdentity
    operation: ScopedOperationIdentity
    output_set_id: str
    binding: OutputSetBinding

    output_type: ClassVar[str]

    def __post_init__(self) -> None:
        if type(self) is CaptureOutput:
            raise OutputError("CaptureOutput must use a concrete variant")
        _identifier(self.output_set_id, "output_set_id")
        if self.operation.tenant_id != self.binding.tenant_id:
            raise OutputError("operation tenant and output tenant differ")

    @property
    def artifact_items(self) -> tuple[ArtifactItem, ...]:
        return ()

    @property
    def payload_bytes(self) -> int:
        return 0

    def to_wire(self) -> dict[str, object]:
        common: dict[str, object] = {
            "contract_version": "capture-output/v1",
            "output_type": self.output_type,
            "output_id": self.identity.output_id,
            "observation_id": self.identity.observation_id,
            "observed_at": self.identity.observed_at.isoformat(),
            "operation": {
                "tenant_id": self.operation.tenant_id,
                "operation_id": self.operation.operation_id,
            },
            "output_set_id": self.output_set_id,
            "binding": binding_to_wire(self.binding),
        }
        if self.identity.natural_key is not None:
            common["natural_key"] = self.identity.natural_key
        if isinstance(self, (RecordOutput, CompositeOutput)):
            common["payload_contract"] = contract_to_wire(self.payload_contract)
            common["payload"] = plain_json(self.payload)
            common["payload_digest"] = canonical_digest(self.payload)
        if isinstance(self, (ArtifactOutput, CompositeOutput)):
            common["artifacts"] = [
                artifact_item_to_wire(item) for item in self.artifacts
            ]
        return common


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordOutput(CaptureOutput):
    output_type: ClassVar[str] = "record"
    payload_contract: PayloadContractRef
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        CaptureOutput.__post_init__(self)
        object.__setattr__(self, "payload", _json_copy(self.payload))
        if not isinstance(self.payload, Mapping):
            raise OutputError(
                "record payload must be a JSON object; empty object is valid"
            )
        if self.payload_bytes > MAX_PAYLOAD_BYTES:
            raise OutputError("record payload exceeds the per-output byte budget")

    @property
    def payload_bytes(self) -> int:
        return len(canonical_json(self.payload))


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactOutput(CaptureOutput):
    output_type: ClassVar[str] = "artifact"
    artifacts: tuple[ArtifactItem, ...]

    def __post_init__(self) -> None:
        CaptureOutput.__post_init__(self)
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        if not self.artifacts:
            raise OutputError(
                "artifact output requires at least one artifact reference"
            )
        if len(self.artifacts) > MAX_ARTIFACTS_PER_OUTPUT:
            raise OutputError("artifact output exceeds the reference count budget")
        ids = [item.artifact.artifact_id for item in self.artifacts]
        if len(ids) != len(set(ids)):
            raise OutputError("artifact references in one output must be unique")

    @property
    def artifact_items(self) -> tuple[ArtifactItem, ...]:
        return self.artifacts


@dataclass(frozen=True, slots=True, kw_only=True)
class CompositeOutput(CaptureOutput):
    output_type: ClassVar[str] = "composite"
    payload_contract: PayloadContractRef
    payload: Mapping[str, object]
    artifacts: tuple[ArtifactItem, ...]

    def __post_init__(self) -> None:
        CaptureOutput.__post_init__(self)
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        object.__setattr__(self, "payload", _json_copy(self.payload))
        if not isinstance(self.payload, Mapping):
            raise OutputError(
                "composite payload must be a JSON object; empty object is valid"
            )
        if self.payload_bytes > MAX_PAYLOAD_BYTES:
            raise OutputError("composite payload exceeds the per-output byte budget")
        if not self.artifacts:
            raise OutputError(
                "composite output requires at least one artifact reference"
            )
        if len(self.artifacts) > MAX_ARTIFACTS_PER_OUTPUT:
            raise OutputError("composite output exceeds the artifact reference budget")
        ids = [item.artifact.artifact_id for item in self.artifacts]
        if len(ids) != len(set(ids)):
            raise OutputError("artifact references in one output must be unique")

    @property
    def payload_bytes(self) -> int:
        return len(canonical_json(self.payload))

    @property
    def artifact_items(self) -> tuple[ArtifactItem, ...]:
        return self.artifacts


type CaptureOutputVariant = RecordOutput | ArtifactOutput | CompositeOutput


@dataclass(frozen=True, slots=True)
class OutputSetBudget:
    max_outputs: int = MAX_OUTPUTS_PER_SET
    max_encoded_bytes: int = MAX_OUTPUT_BYTES_PER_SET
    max_output_bytes: int = MAX_PAYLOAD_BYTES

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (
                self.max_outputs,
                self.max_encoded_bytes,
                self.max_output_bytes,
            )
        ):
            raise OutputError("OutputSet budgets must be integer values")
        if not 1 <= self.max_outputs <= MAX_OUTPUTS_PER_SET:
            raise OutputError("max_outputs is outside supported bounds")
        if not 1 <= self.max_encoded_bytes <= MAX_OUTPUT_BYTES_PER_SET:
            raise OutputError("max_encoded_bytes is outside supported bounds")
        if not 1 <= self.max_output_bytes <= MAX_PAYLOAD_BYTES:
            raise OutputError("max_output_bytes is outside supported bounds")


@dataclass(frozen=True, slots=True)
class OutputSetSpec:
    output_set_id: str
    binding: OutputSetBinding
    payload_contracts: tuple[PayloadContractRef, ...] = ()
    expected_output_count: int | None = None
    budget: OutputSetBudget = field(default_factory=OutputSetBudget)

    def __post_init__(self) -> None:
        _identifier(self.output_set_id, "output_set_id")
        object.__setattr__(
            self,
            "payload_contracts",
            tuple(
                sorted(self.payload_contracts, key=lambda c: (c.contract_id, c.version))
            ),
        )
        if len(self.payload_contracts) > MAX_CONTRACTS_PER_SET:
            raise OutputError("OutputSet declares too many payload contracts")
        if self.expected_output_count is not None and (
            isinstance(self.expected_output_count, bool)
            or not isinstance(self.expected_output_count, int)
        ):
            raise OutputError("expected_output_count must be an integer")
        if (
            self.expected_output_count is not None
            and not 0 <= self.expected_output_count <= self.budget.max_outputs
        ):
            raise OutputError("expected_output_count exceeds set budget")
        keys = [(item.contract_id, item.version) for item in self.payload_contracts]
        if len(keys) != len(set(keys)):
            raise OutputError("payload contracts must be unique by id and version")


@dataclass(frozen=True, slots=True)
class OutputSet:
    """Aggregate root; stores bounded counters/digests, not output payload members."""

    spec: OutputSetSpec
    state: OutputSetState = OutputSetState.STAGING
    member_count: int = 0
    encoded_output_bytes: int = 0
    artifact_reference_count: int = 0
    custody_reference_count: int = 0
    manifest_digest: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sealed_at: datetime | None = None
    published_at: datetime | None = None
    terminal_reason: str | None = None

    @property
    def output_set_id(self) -> str:
        return self.spec.output_set_id

    @property
    def binding(self) -> OutputSetBinding:
        return self.spec.binding

    def validate_member(self, output: CaptureOutputVariant) -> None:
        if self.state != OutputSetState.STAGING:
            raise InvalidTransition(
                "outputs can only be staged while OutputSet is staging"
            )
        if output.output_set_id != self.output_set_id:
            raise OutputConflict("output references another OutputSet")
        if output.binding != self.binding:
            raise OutputConflict(
                "output tenant/source/producer/execution binding differs from OutputSet"
            )
        if output.operation.tenant_id != self.binding.tenant_id:
            raise OutputConflict("output operation belongs to another tenant")
        if (
            isinstance(output, (RecordOutput, CompositeOutput))
            and output.payload_contract not in self.spec.payload_contracts
        ):
            raise OutputConflict("output payload contract is not declared by OutputSet")
        if output.payload_bytes > self.spec.budget.max_output_bytes:
            raise OutputError("output exceeds OutputSet per-output byte budget")

    def record_staged(
        self, output: CaptureOutputVariant, *, custody_receipts: int
    ) -> OutputSet:
        self.validate_member(output)
        if self.member_count + 1 > self.spec.budget.max_outputs:
            raise OutputError("OutputSet exceeds member count budget")
        payload_bytes = len(canonical_json(output.to_wire()))
        if (
            self.encoded_output_bytes + payload_bytes
            > self.spec.budget.max_encoded_bytes
        ):
            raise OutputError("OutputSet exceeds encoded byte budget")
        artifact_count = len(output.artifact_items)
        if custody_receipts != artifact_count:
            raise OutputError(
                "each artifact reference requires a matching custody receipt"
            )
        return replace(
            self,
            member_count=self.member_count + 1,
            encoded_output_bytes=self.encoded_output_bytes + payload_bytes,
            artifact_reference_count=self.artifact_reference_count + artifact_count,
            custody_reference_count=self.custody_reference_count + custody_receipts,
        )

    def seal(
        self,
        *,
        member_count: int,
        encoded_output_bytes: int,
        manifest_digest: str,
        at: datetime,
    ) -> OutputSet:
        if self.state == OutputSetState.SEALED:
            if self.manifest_digest == manifest_digest and (
                member_count,
                encoded_output_bytes,
            ) == (self.member_count, self.encoded_output_bytes):
                return self
            raise OutputConflict("OutputSet was sealed with a different manifest")
        if self.state != OutputSetState.STAGING:
            raise InvalidTransition("only a staging OutputSet can be sealed")
        _digest(manifest_digest, "manifest_digest")
        if (member_count, encoded_output_bytes) != (
            self.member_count,
            self.encoded_output_bytes,
        ):
            raise OutputConflict(
                "sealed manifest counters differ from staged membership"
            )
        if (
            self.spec.expected_output_count is not None
            and member_count != self.spec.expected_output_count
        ):
            raise OutputConflict("staged membership differs from expected_output_count")
        return replace(
            self,
            state=OutputSetState.SEALED,
            manifest_digest=manifest_digest,
            sealed_at=at,
        )

    def publish(self, *, manifest_digest: str, at: datetime) -> OutputSet:
        _digest(manifest_digest, "manifest_digest")
        if self.state == OutputSetState.PUBLISHED:
            if self.manifest_digest == manifest_digest:
                return self
            raise OutputConflict("published OutputSet manifest cannot change")
        if self.state != OutputSetState.SEALED:
            raise InvalidTransition("only a sealed OutputSet can be published")
        if self.manifest_digest != manifest_digest:
            raise OutputConflict("publication manifest differs from sealed membership")
        if self.artifact_reference_count != self.custody_reference_count:
            raise OutputError(
                "OutputSet has artifact references without confirmed custody"
            )
        return replace(self, state=OutputSetState.PUBLISHED, published_at=at)

    def abort(self, reason: str) -> OutputSet:
        return self._terminate(OutputSetState.ABORTED, reason)

    def reject(self, reason: str) -> OutputSet:
        return self._terminate(OutputSetState.REJECTED, reason)

    def _terminate(self, state: OutputSetState, reason: str) -> OutputSet:
        if state not in {OutputSetState.ABORTED, OutputSetState.REJECTED}:
            raise OutputError("invalid terminal state")
        if self.state in {OutputSetState.ABORTED, OutputSetState.REJECTED}:
            if self.state == state and self.terminal_reason == reason:
                return self
            raise OutputConflict("OutputSet already has a different terminal result")
        if self.state == OutputSetState.PUBLISHED:
            raise InvalidTransition("published OutputSet cannot be aborted or rejected")
        _identifier(reason, "terminal reason", maximum=1024)
        return replace(self, state=state, terminal_reason=reason)


@dataclass(frozen=True, slots=True)
class MemberManifestEntry:
    output_id: str
    observation_id: str
    operation_id: str
    intent_digest: str

    def __post_init__(self) -> None:
        _identifier(self.output_id, "member output_id")
        _identifier(self.observation_id, "member observation_id")
        _identifier(self.operation_id, "member operation_id")
        _digest(self.intent_digest, "member intent_digest")


@dataclass(frozen=True, slots=True)
class OutputSetManifest:
    output_set_id: str
    binding: OutputSetBinding
    state: OutputSetState
    payload_contracts: tuple[PayloadContractRef, ...]
    member_count: int
    encoded_output_bytes: int
    artifact_reference_count: int
    custody_reference_count: int
    members: tuple[MemberManifestEntry, ...]
    manifest_digest: str | None
    created_at: datetime
    sealed_at: datetime | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class OutputSetSnapshot:
    """Bounded operational view; membership is read separately and incrementally."""

    output_set_id: str
    binding: OutputSetBinding
    state: OutputSetState
    payload_contracts: tuple[PayloadContractRef, ...]
    member_count: int
    encoded_output_bytes: int
    artifact_reference_count: int
    custody_reference_count: int
    manifest_digest: str | None
    created_at: datetime
    sealed_at: datetime | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class OutputReceipt:
    tenant_id: str
    operation_id: str
    output_set_id: str
    output_id: str
    intent_digest: str
    status: str = "staged"

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "receipt tenant")
        _identifier(self.operation_id, "receipt operation_id")
        _identifier(self.output_set_id, "receipt output_set_id")
        _identifier(self.output_id, "receipt output_id")
        _digest(self.intent_digest, "receipt intent_digest")
        if self.status != "staged":
            raise OutputError("unsupported OutputReceipt status")


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    tenant_id: str
    operation_id: str
    output_set_id: str
    manifest_digest: str
    publication_id: str
    published_at: datetime

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "publication tenant")
        _identifier(self.operation_id, "publication operation_id")
        _identifier(self.output_set_id, "publication output_set_id")
        _digest(self.manifest_digest, "publication manifest_digest")
        _identifier(self.publication_id, "publication_id")


@dataclass(frozen=True, slots=True)
class CustodyReceipt:
    tenant_id: str
    artifact_id: str
    digest: str
    size_bytes: int
    receipt_id: str

    def __post_init__(self) -> None:
        _identifier(self.tenant_id, "custody tenant")
        _identifier(self.artifact_id, "custody artifact id")
        _identifier(self.receipt_id, "custody receipt id")
        _digest(self.digest, "custody digest")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or not 0 <= self.size_bytes <= SAFE_INTEGER
        ):
            raise OutputError(
                "custody receipt size must be a non-negative interoperable JSON integer"
            )


def contract_to_wire(contract: PayloadContractRef) -> dict[str, str]:
    return {
        "contract_id": contract.contract_id,
        "version": contract.version,
        "digest": contract.digest,
    }


def producer_to_wire(producer: ProducerIdentity) -> dict[str, str]:
    return {
        key: value
        for key, value in (
            ("producer_id", producer.producer_id),
            ("producer_version", producer.producer_version),
            ("capability_id", producer.capability_id),
            ("capability_version", producer.capability_version),
            ("release_id", producer.release_id),
            ("release_digest", producer.release_digest),
        )
        if value is not None
    }


def binding_to_wire(binding: OutputSetBinding) -> dict[str, object]:
    execution: dict[str, object] = {}
    if binding.execution.run_id is not None:
        execution["run_id"] = binding.execution.run_id
        execution["attempt_id"] = binding.execution.attempt_id
    if binding.execution.fencing_token is not None:
        execution["fencing_token"] = binding.execution.fencing_token
    return {
        "tenant_id": binding.tenant_id,
        "source": {
            "source_id": binding.source.source_id,
            "revision": binding.source.revision,
        },
        "producer": producer_to_wire(binding.producer),
        "execution": execution,
    }


def artifact_item_to_wire(item: ArtifactItem) -> dict[str, object]:
    artifact: dict[str, object] = {
        "artifact_id": item.artifact.artifact_id,
        "digest": item.artifact.digest,
        "size_bytes": item.artifact.size_bytes,
    }
    if item.artifact.media_type is not None:
        artifact["media_type"] = item.artifact.media_type
    result: dict[str, object] = {"artifact": artifact}
    if item.description is not None:
        result["description"] = item.description.to_wire()
    return result


def output_intent_digest(output: CaptureOutputVariant) -> str:
    return canonical_digest(output.to_wire())


def output_set_intent_digest(spec: OutputSetSpec) -> str:
    return canonical_digest(
        {
            "output_set_id": spec.output_set_id,
            "binding": binding_to_wire(spec.binding),
            "payload_contracts": [
                contract_to_wire(item)
                for item in sorted(
                    spec.payload_contracts, key=lambda c: (c.contract_id, c.version)
                )
            ],
            "expected_output_count": spec.expected_output_count,
            "budget": {
                "max_outputs": spec.budget.max_outputs,
                "max_encoded_bytes": spec.budget.max_encoded_bytes,
                "max_output_bytes": spec.budget.max_output_bytes,
            },
        }
    )


def manifest_digest(manifest: OutputSetManifest) -> str:
    body = {
        "manifest_version": "output-set-manifest/v1",
        "output_set_id": manifest.output_set_id,
        "binding": binding_to_wire(manifest.binding),
        "payload_contracts": [
            contract_to_wire(item)
            for item in sorted(
                manifest.payload_contracts, key=lambda c: (c.contract_id, c.version)
            )
        ],
        "member_count": manifest.member_count,
        "encoded_output_bytes": manifest.encoded_output_bytes,
        "artifact_reference_count": manifest.artifact_reference_count,
        "custody_reference_count": manifest.custody_reference_count,
        "members": [
            {
                "output_id": item.output_id,
                "observation_id": item.observation_id,
                "operation_id": item.operation_id,
                "intent_digest": item.intent_digest,
            }
            for item in sorted(
                manifest.members, key=lambda m: (m.output_id, m.operation_id)
            )
        ],
    }
    return canonical_digest(body)


def stream_manifest_digest(
    snapshot: OutputSetSnapshot,
    members: Iterable[MemberManifestEntry],
) -> str:
    """Hash the v1 manifest bytes without collecting its member list in memory.

    `members` must be ordered by `(output_id, operation_id)`, matching the
    original v1 projection. The returned digest is byte-for-byte compatible
    with `manifest_digest` for the same membership.
    """
    fields: dict[str, object] = {
        "manifest_version": "output-set-manifest/v1",
        "output_set_id": snapshot.output_set_id,
        "binding": binding_to_wire(snapshot.binding),
        "payload_contracts": [
            contract_to_wire(item)
            for item in sorted(
                snapshot.payload_contracts, key=lambda c: (c.contract_id, c.version)
            )
        ],
        "member_count": snapshot.member_count,
        "encoded_output_bytes": snapshot.encoded_output_bytes,
        "artifact_reference_count": snapshot.artifact_reference_count,
        "custody_reference_count": snapshot.custody_reference_count,
    }
    digest = hashlib.sha256()
    digest.update(b"{")
    keys = sorted((*fields.keys(), "members"))
    for position, key in enumerate(keys):
        if position:
            digest.update(b",")
        digest.update(canonical_json(key))
        digest.update(b":")
        if key != "members":
            digest.update(canonical_json(fields[key]))
            continue
        digest.update(b"[")
        count = 0
        previous_key: tuple[str, str] | None = None
        for member in members:
            member_key = (member.output_id, member.operation_id)
            if previous_key is not None and member_key < previous_key:
                raise OutputError("manifest members are not in deterministic order")
            if count:
                digest.update(b",")
            digest.update(
                canonical_json(
                    {
                        "output_id": member.output_id,
                        "observation_id": member.observation_id,
                        "operation_id": member.operation_id,
                        "intent_digest": member.intent_digest,
                    }
                )
            )
            previous_key = member_key
            count += 1
        if count != snapshot.member_count:
            raise OutputConflict("manifest member stream count differs from snapshot")
        digest.update(b"]")
    digest.update(b"}")
    return "sha256:" + digest.hexdigest()


def strict_json_loads(document: str | bytes) -> object:
    """Parse JSON while rejecting duplicate object names and non-JSON constants."""

    def object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise OutputError(f"duplicate JSON property: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise OutputError(f"non-finite JSON number is not supported: {value}")

    try:
        return json.loads(
            document,
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise OutputError("input is not valid UTF-8 JSON") from error
