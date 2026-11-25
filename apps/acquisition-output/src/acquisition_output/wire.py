"""Explicit public-contract projections; no aggregate is serialized directly."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from .domain import (
    ArtifactDescription,
    ArtifactItem,
    ArtifactOutput,
    ArtifactRef,
    CaptureOutputVariant,
    CompositeOutput,
    ExecutionBinding,
    OutputError,
    OutputIdentity,
    OutputReceipt,
    OutputSetBinding,
    OutputSetManifest,
    PayloadContractRef,
    ProducerIdentity,
    PublicationReceipt,
    RecordOutput,
    ScopedOperationIdentity,
    SourceRevisionRef,
    binding_to_wire,
    canonical_digest,
    contract_to_wire,
    strict_json_loads,
)


def capture_output_from_wire(value: Mapping[str, object]) -> CaptureOutputVariant:
    """Map a validated public contract document to a private domain value.

    This parser still rejects unknown/missing fields and checks domain invariants;
    JSON Schema validation and payload contract validation remain application-edge
    responsibilities.
    """
    if (
        not isinstance(value, Mapping)
        or value.get("contract_version") != "capture-output/v1"
    ):
        raise OutputError("unsupported CaptureOutput wire contract")
    output_type = value.get("output_type")
    common = {
        "contract_version",
        "output_type",
        "output_id",
        "observation_id",
        "observed_at",
        "natural_key",
        "operation",
        "output_set_id",
        "binding",
    }
    payload_keys = {"payload_contract", "payload", "payload_digest"}
    artifact_keys = {"artifacts"}
    if output_type == "record":
        expected = common | payload_keys
        cls = RecordOutput
    elif output_type == "artifact":
        expected = common | artifact_keys
        cls = ArtifactOutput
    elif output_type == "composite":
        expected = common | payload_keys | artifact_keys
        cls = CompositeOutput
    else:
        raise OutputError("unknown CaptureOutput variant")
    if frozenset(value) not in {
        frozenset(expected),
        frozenset(expected - {"natural_key"}),
    }:
        raise OutputError("CaptureOutput fields do not match its declared variant")
    raw_operation = _object(value["operation"], "operation")
    raw_binding = _object(value["binding"], "binding")
    _require_fields(raw_operation, {"tenant_id", "operation_id"}, set(), "operation")
    _require_fields(
        raw_binding, {"tenant_id", "source", "producer", "execution"}, set(), "binding"
    )
    raw_source = _object(raw_binding.get("source"), "binding.source")
    raw_producer = _object(raw_binding.get("producer"), "binding.producer")
    raw_execution = _object(raw_binding.get("execution"), "binding.execution")
    _require_fields(raw_source, {"source_id", "revision"}, set(), "binding.source")
    _require_fields(
        raw_producer,
        {"producer_id", "producer_version"},
        {"capability_id", "capability_version", "release_id", "release_digest"},
        "binding.producer",
    )
    _require_fields(
        raw_execution,
        set(),
        {"run_id", "attempt_id", "fencing_token"},
        "binding.execution",
    )
    binding = OutputSetBinding(
        tenant_id=_string(raw_binding.get("tenant_id"), "binding.tenant_id"),
        source=SourceRevisionRef(
            _string(raw_source.get("source_id"), "binding.source.source_id"),
            _string(raw_source.get("revision"), "binding.source.revision"),
        ),
        producer=ProducerIdentity(
            producer_id=_string(
                raw_producer.get("producer_id"), "binding.producer.producer_id"
            ),
            producer_version=_string(
                raw_producer.get("producer_version"),
                "binding.producer.producer_version",
            ),
            capability_id=_optional_string_field(
                raw_producer, "capability_id", "binding.producer.capability_id"
            ),
            capability_version=_optional_string_field(
                raw_producer,
                "capability_version",
                "binding.producer.capability_version",
            ),
            release_id=_optional_string_field(
                raw_producer, "release_id", "binding.producer.release_id"
            ),
            release_digest=_optional_string_field(
                raw_producer, "release_digest", "binding.producer.release_digest"
            ),
        ),
        execution=ExecutionBinding(
            run_id=_optional_string_field(
                raw_execution, "run_id", "binding.execution.run_id"
            ),
            attempt_id=_optional_string_field(
                raw_execution, "attempt_id", "binding.execution.attempt_id"
            ),
            fencing_token=_optional_int_field(
                raw_execution, "fencing_token", "binding.execution.fencing_token"
            ),
        ),
    )
    identity = OutputIdentity(
        output_id=_string(value.get("output_id"), "output_id"),
        observation_id=_string(value.get("observation_id"), "observation_id"),
        observed_at=_datetime(value.get("observed_at"), "observed_at"),
        natural_key=_optional_string_field(value, "natural_key", "natural_key"),
    )
    operation = ScopedOperationIdentity(
        _string(raw_operation.get("tenant_id"), "operation.tenant_id"),
        _string(raw_operation.get("operation_id"), "operation.operation_id"),
    )
    common_args = {
        "identity": identity,
        "operation": operation,
        "output_set_id": _string(value.get("output_set_id"), "output_set_id"),
        "binding": binding,
    }
    artifacts = (
        tuple(
            _artifact_item(item) for item in _array(value.get("artifacts"), "artifacts")
        )
        if output_type in {"artifact", "composite"}
        else ()
    )
    if output_type in {"record", "composite"}:
        payload = value.get("payload")
        contract_doc = _object(value.get("payload_contract"), "payload_contract")
        _require_fields(
            contract_doc,
            {"contract_id", "version", "digest"},
            set(),
            "payload_contract",
        )
        contract = PayloadContractRef(
            _string(contract_doc.get("contract_id"), "payload_contract.contract_id"),
            _string(contract_doc.get("version"), "payload_contract.version"),
            _string(contract_doc.get("digest"), "payload_contract.digest"),
        )
        if not isinstance(payload, Mapping):
            raise OutputError("payload must be an object")
        output = cls(
            **common_args,
            payload_contract=contract,
            payload=payload,
            **({"artifacts": artifacts} if output_type == "composite" else {}),
        )
        if value.get("payload_digest") != canonical_digest(output.payload):
            raise OutputError("payload_digest does not match canonical payload")
        return output
    return ArtifactOutput(**common_args, artifacts=artifacts)


def capture_output_from_json(document: str | bytes) -> CaptureOutputVariant:
    """Strict raw-JSON boundary that rejects duplicate properties before mapping."""
    value = strict_json_loads(document)
    if not isinstance(value, Mapping):
        raise OutputError("CaptureOutput JSON document must be an object")
    return capture_output_from_wire(value)


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OutputError(f"{label} must be an object")
    return value


def _require_fields(
    value: Mapping[str, object], required: set[str], optional: set[str], label: str
) -> None:
    keys = set(value)
    if not required <= keys or not keys <= required | optional:
        raise OutputError(f"{label} fields are missing or unknown")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise OutputError(f"{label} must be a string")
    return value


def _optional_string_field(
    fields: Mapping[str, object], name: str, label: str
) -> str | None:
    if name not in fields:
        return None
    return _string(fields[name], label)


def _optional_int_field(
    fields: Mapping[str, object], name: str, label: str
) -> int | None:
    if name not in fields:
        return None
    value = fields[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise OutputError(f"{label} must be an integer")
    return value


def _datetime(value: object, label: str) -> datetime:
    raw = _string(value, label)
    try:
        return datetime.fromisoformat(raw)
    except ValueError as error:
        raise OutputError(f"{label} must be an RFC 3339 date-time") from error


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise OutputError(f"{label} must be an array")
    return value


def _artifact_item(value: object) -> ArtifactItem:
    item = _object(value, "artifact item")
    _require_fields(item, {"artifact"}, {"description"}, "artifact item")
    artifact = _object(item.get("artifact"), "artifact item.artifact")
    _require_fields(
        artifact, {"artifact_id", "digest", "size_bytes"}, {"media_type"}, "artifact"
    )
    description_value = item.get("description")
    description = None
    if "description" in item:
        fields = _object(description_value, "artifact description")
        allowed = {"file_name", "title", "description", "role"}
        _require_fields(fields, set(), allowed, "artifact description")
        if any(not isinstance(value, str) for value in fields.values()):
            raise OutputError(
                "artifact description fields are strings; omit absent values"
            )
        description = ArtifactDescription(**fields)
    return ArtifactItem(
        ArtifactRef(
            artifact_id=_string(artifact.get("artifact_id"), "artifact_id"),
            digest=_string(artifact.get("digest"), "artifact.digest"),
            size_bytes=_optional_int_field(
                artifact, "size_bytes", "artifact.size_bytes"
            ),
            media_type=_optional_string_field(
                artifact, "media_type", "artifact.media_type"
            ),
        ),
        description,
    )


def output_set_manifest_to_wire(manifest: OutputSetManifest) -> dict[str, object]:
    return {
        "contract_version": "output-set-manifest/v1",
        "output_set_id": manifest.output_set_id,
        "binding": binding_to_wire(manifest.binding),
        "state": manifest.state.value,
        "payload_contracts": [
            contract_to_wire(item) for item in manifest.payload_contracts
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
            for item in manifest.members
        ],
        "manifest_digest": manifest.manifest_digest,
        "created_at": manifest.created_at.isoformat(),
        "sealed_at": manifest.sealed_at.isoformat() if manifest.sealed_at else None,
        "published_at": manifest.published_at.isoformat()
        if manifest.published_at
        else None,
    }


def output_receipt_to_wire(receipt: OutputReceipt) -> dict[str, object]:
    return {
        "contract_version": "output-receipt/v1",
        "tenant_id": receipt.tenant_id,
        "operation_id": receipt.operation_id,
        "output_set_id": receipt.output_set_id,
        "output_id": receipt.output_id,
        "intent_digest": receipt.intent_digest,
        "status": receipt.status,
    }


def publication_receipt_to_wire(receipt: PublicationReceipt) -> dict[str, object]:
    result: dict[str, object] = {
        "contract_version": "publication-receipt/v1",
        "tenant_id": receipt.tenant_id,
        "operation_id": receipt.operation_id,
        "output_set_id": receipt.output_set_id,
        "manifest_digest": receipt.manifest_digest,
        "publication_id": receipt.publication_id,
        "published_at": receipt.published_at.isoformat(),
    }
    if receipt.finalization_reservation_id is not None:
        result["finalization_reservation_id"] = receipt.finalization_reservation_id
    return result
