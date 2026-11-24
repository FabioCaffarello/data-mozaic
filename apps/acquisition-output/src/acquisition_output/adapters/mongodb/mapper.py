"""Explicit private-domain to BSON mappings for BC05 documents."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from bson import BSON
from bson.errors import InvalidDocument

from acquisition_output.domain import (
    CaptureOutputVariant,
    CustodyReceipt,
    ExecutionBinding,
    OutputConflict,
    OutputError,
    OutputReceipt,
    OutputSet,
    OutputSetBudget,
    OutputSetSnapshot,
    OutputSetSpec,
    OutputSetState,
    PayloadContractRef,
    ProducerIdentity,
    PublicationReceipt,
    SourceRevisionRef,
    canonical_json,
    contract_to_wire,
    output_intent_digest,
)
from acquisition_output.wire import capture_output_from_wire

MAX_BSON_DOCUMENT_BYTES = 16 * 1024 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def bson_size(document: dict[str, Any]) -> int:
    try:
        return len(BSON.encode(document))
    except (InvalidDocument, OverflowError, TypeError) as error:
        raise OutputError(
            f"BC05 document cannot be encoded as BSON: {type(error).__name__}"
        ) from error


def require_bson_budget(document: dict[str, Any]) -> int:
    size = bson_size(document)
    if size > MAX_BSON_DOCUMENT_BYTES:
        raise OutputError("BC05 document exceeds MongoDB's 16 MiB BSON limit")
    return size


def datetime_to_epoch_us(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OutputError("persisted domain timestamps must be timezone-aware")
    delta = value.astimezone(UTC) - _EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000) + delta.microseconds


def datetime_from_epoch_us(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OutputError("BSON timestamp mapping must be an integer microsecond value")
    return _EPOCH + timedelta(microseconds=value)


def contract_from_document(value: dict[str, Any]) -> PayloadContractRef:
    return PayloadContractRef(value["contract_id"], value["version"], value["digest"])


def binding_to_document(binding: Any) -> dict[str, Any]:
    return {
        "tenant_id": binding.tenant_id,
        "source": {
            "source_id": binding.source.source_id,
            "revision": binding.source.revision,
        },
        "producer": {
            key: value
            for key, value in (
                ("producer_id", binding.producer.producer_id),
                ("producer_version", binding.producer.producer_version),
                ("capability_id", binding.producer.capability_id),
                ("capability_version", binding.producer.capability_version),
                ("release_id", binding.producer.release_id),
                ("release_digest", binding.producer.release_digest),
            )
            if value is not None
        },
        "execution": {
            key: value
            for key, value in (
                ("run_id", binding.execution.run_id),
                ("attempt_id", binding.execution.attempt_id),
                ("fencing_token", binding.execution.fencing_token),
            )
            if value is not None
        },
    }


def binding_from_document(value: dict[str, Any]):
    from acquisition_output.domain import OutputSetBinding

    producer = value["producer"]
    execution = value["execution"]
    source = value["source"]
    return OutputSetBinding(
        tenant_id=value["tenant_id"],
        source=SourceRevisionRef(source["source_id"], source["revision"]),
        producer=ProducerIdentity(
            producer_id=producer["producer_id"],
            producer_version=producer["producer_version"],
            capability_id=producer.get("capability_id"),
            capability_version=producer.get("capability_version"),
            release_id=producer.get("release_id"),
            release_digest=producer.get("release_digest"),
        ),
        execution=ExecutionBinding(
            run_id=execution.get("run_id"),
            attempt_id=execution.get("attempt_id"),
            fencing_token=execution.get("fencing_token"),
        ),
    )


def spec_to_document(spec: OutputSetSpec) -> dict[str, Any]:
    return {
        "payload_contracts": [
            contract_to_wire(item) for item in spec.payload_contracts
        ],
        "expected_output_count": spec.expected_output_count,
        "budget": {
            "max_outputs": spec.budget.max_outputs,
            "max_encoded_bytes": spec.budget.max_encoded_bytes,
            "max_output_bytes": spec.budget.max_output_bytes,
        },
    }


def spec_from_document(document: dict[str, Any]) -> OutputSetSpec:
    spec = document["spec"]
    budget = spec["budget"]
    return OutputSetSpec(
        output_set_id=document["output_set_id"],
        binding=binding_from_document(document["binding"]),
        payload_contracts=tuple(
            contract_from_document(item) for item in spec["payload_contracts"]
        ),
        expected_output_count=spec.get("expected_output_count"),
        budget=OutputSetBudget(
            max_outputs=budget["max_outputs"],
            max_encoded_bytes=budget["max_encoded_bytes"],
            max_output_bytes=budget["max_output_bytes"],
        ),
    )


def root_from_document(document: dict[str, Any]) -> OutputSet:
    counters = document["counters"]
    return OutputSet(
        spec=spec_from_document(document),
        state=OutputSetState(document["state"]),
        member_count=counters["member_count"],
        encoded_output_bytes=counters["encoded_output_bytes"],
        artifact_reference_count=counters["artifact_reference_count"],
        custody_reference_count=counters["custody_reference_count"],
        manifest_digest=document.get("manifest_digest"),
        created_at=datetime_from_epoch_us(document["created_at_us"]),
        sealed_at=datetime_from_epoch_us(document["sealed_at_us"])
        if document.get("sealed_at_us") is not None
        else None,
        published_at=datetime_from_epoch_us(document["published_at_us"])
        if document.get("published_at_us") is not None
        else None,
        terminal_reason=document.get("terminal_reason"),
    )


def snapshot_from_document(document: dict[str, Any]) -> OutputSetSnapshot:
    root = root_from_document(document)
    return OutputSetSnapshot(
        output_set_id=root.output_set_id,
        binding=root.binding,
        state=root.state,
        payload_contracts=root.spec.payload_contracts,
        member_count=root.member_count,
        encoded_output_bytes=root.encoded_output_bytes,
        artifact_reference_count=root.artifact_reference_count,
        custody_reference_count=root.custody_reference_count,
        manifest_digest=root.manifest_digest,
        created_at=root.created_at,
        sealed_at=root.sealed_at,
        published_at=root.published_at,
    )


def set_document(root: OutputSet, *, revision: int = 0) -> dict[str, Any]:
    return {
        "_id": set_storage_id(root.binding.tenant_id, root.output_set_id),
        "tenant_id": root.binding.tenant_id,
        "output_set_id": root.output_set_id,
        "binding": binding_to_document(root.binding),
        "spec": spec_to_document(root.spec),
        "state": root.state.value,
        "revision": revision,
        "counters": {
            "member_count": root.member_count,
            "encoded_output_bytes": root.encoded_output_bytes,
            "artifact_reference_count": root.artifact_reference_count,
            "custody_reference_count": root.custody_reference_count,
        },
        "manifest_digest": root.manifest_digest,
        "created_at_us": datetime_to_epoch_us(root.created_at),
        "sealed_at_us": datetime_to_epoch_us(root.sealed_at)
        if root.sealed_at
        else None,
        "published_at_us": datetime_to_epoch_us(root.published_at)
        if root.published_at
        else None,
        "terminal_reason": root.terminal_reason,
    }


def output_document(
    output: CaptureOutputVariant,
    intent_digest: str,
    custody: tuple[CustodyReceipt, ...],
) -> dict[str, Any]:
    wire = output.to_wire()
    document = {
        "_id": output_storage_id(
            output.binding.tenant_id, output.output_set_id, output.identity.output_id
        ),
        "tenant_id": output.binding.tenant_id,
        "output_set_id": output.output_set_id,
        "output_id": output.identity.output_id,
        "observation_id": output.identity.observation_id,
        "operation_id": output.operation.operation_id,
        "intent_digest": intent_digest,
        "canonical_output_bytes": len(canonical_json(wire)),
        "artifact_reference_count": len(output.artifact_items),
        "custody_reference_count": len(custody),
        "wire": wire,
        "custody_receipts": [
            {
                "artifact_id": item.artifact_id,
                "digest": item.digest,
                "size_bytes": item.size_bytes,
                "receipt_id": item.receipt_id,
            }
            for item in custody
        ],
    }
    if "payload_digest" in wire:
        document["payload_digest"] = wire["payload_digest"]
    return document


def output_from_document(document: dict[str, Any]) -> CaptureOutputVariant:
    output = capture_output_from_wire(document["wire"])
    if (
        output.binding.tenant_id != document["tenant_id"]
        or output.output_set_id != document["output_set_id"]
        or output.identity.output_id != document["output_id"]
        or output.identity.observation_id != document["observation_id"]
        or output.operation.operation_id != document["operation_id"]
        or output_intent_digest(output) != document["intent_digest"]
    ):
        raise OutputConflict(
            "stored BSON envelope disagrees with its indexed identity or digest"
        )
    actual_bytes = len(canonical_json(output.to_wire()))
    if actual_bytes != document["canonical_output_bytes"]:
        raise OutputConflict(
            "stored BSON output does not round-trip to canonical bytes"
        )
    return output


def custody_from_document(
    document: dict[str, Any], tenant_id: str
) -> tuple[CustodyReceipt, ...]:
    return tuple(
        CustodyReceipt(
            tenant_id=tenant_id,
            artifact_id=item["artifact_id"],
            digest=item["digest"],
            size_bytes=item["size_bytes"],
            receipt_id=item["receipt_id"],
        )
        for item in document["custody_receipts"]
    )


def output_receipt_document(receipt: OutputReceipt) -> dict[str, Any]:
    return {
        "tenant_id": receipt.tenant_id,
        "operation_id": receipt.operation_id,
        "output_set_id": receipt.output_set_id,
        "output_id": receipt.output_id,
        "intent_digest": receipt.intent_digest,
        "status": receipt.status,
    }


def output_receipt_from_document(document: dict[str, Any]) -> OutputReceipt:
    return OutputReceipt(**document)


def publication_receipt_document(receipt: PublicationReceipt) -> dict[str, Any]:
    return {
        "tenant_id": receipt.tenant_id,
        "operation_id": receipt.operation_id,
        "output_set_id": receipt.output_set_id,
        "manifest_digest": receipt.manifest_digest,
        "publication_id": receipt.publication_id,
        "published_at_us": datetime_to_epoch_us(receipt.published_at),
    }


def publication_receipt_from_document(document: dict[str, Any]) -> PublicationReceipt:
    return PublicationReceipt(
        tenant_id=document["tenant_id"],
        operation_id=document["operation_id"],
        output_set_id=document["output_set_id"],
        manifest_digest=document["manifest_digest"],
        publication_id=document["publication_id"],
        published_at=datetime_from_epoch_us(document["published_at_us"]),
    )


def operation_storage_id(tenant_id: str, operation_id: str) -> str:
    return _storage_id("op", tenant_id, operation_id)


def set_storage_id(tenant_id: str, output_set_id: str) -> str:
    return _storage_id("set", tenant_id, output_set_id)


def output_storage_id(tenant_id: str, output_set_id: str, output_id: str) -> str:
    return _storage_id("output", tenant_id, output_set_id, output_id)


def _storage_id(kind: str, *parts: str) -> str:
    from hashlib import sha256

    from acquisition_output.domain import canonical_digest

    return (
        f"{kind}-" + sha256(canonical_digest(list(parts)).encode("ascii")).hexdigest()
    )
