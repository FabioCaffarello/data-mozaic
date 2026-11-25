"""Read-only trusted execution context adapter for BC04's published port."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Protocol

from execution_coordination.durable_domain import DurableRunSnapshot


class CurrentExecutionReader(Protocol):
    def get(self, tenant_id: str, run_id: str) -> DurableRunSnapshot | None: ...


class AcquisitionApplicationError(ValueError):
    """Trusted acquisition context or result is invalid."""


@dataclass(frozen=True, slots=True)
class TrustedExecutionContext:
    tenant_id: str
    run_id: str
    request_id: str
    attempt_id: str
    generation: int
    source_id: str
    source_revision_id: str
    bot_id: str | None
    build_id: str
    build_manifest_digest: str
    runtime_image_digest: str
    policy_digest: str | None
    maximum_total_response_bytes: int
    maximum_requests: int
    release_id: str
    created_at: datetime
    deadline: datetime
    execution_mode: str
    binding_digest: str
    replay_output_set_id: str | None = None
    replay_manifest_digest: str | None = None


def load_current_execution(
    reader: CurrentExecutionReader,
    tenant_id: str,
    run_id: str,
    *,
    allow_published_replay: bool = False,
) -> TrustedExecutionContext:
    """Resolve current identities from BC04 rather than worker declarations."""
    snapshot = reader.get(tenant_id, run_id)
    if (
        snapshot is None
        or snapshot.intent.tenant_id != tenant_id
        or snapshot.intent.run_id != run_id
    ):
        raise AcquisitionApplicationError("BC04 execution does not exist in tenant")
    attempt_id = snapshot.attempt_id
    reservation = snapshot.reservation
    receipt = snapshot.publication_receipt
    completed_replay = (
        allow_published_replay
        and snapshot.state.value == "succeeded"
        and snapshot.attempt_state is not None
        and snapshot.attempt_state.value == "succeeded"
        and reservation is not None
        and receipt is not None
        and reservation.tenant_id == tenant_id
        and reservation.run_id == run_id
        and reservation.attempt_id == attempt_id
        and reservation.fencing_generation == snapshot.generation
        and receipt.get("tenant_id") == tenant_id
        and receipt.get("output_set_id") == reservation.output_set_id
        and receipt.get("manifest_digest") == reservation.manifest_digest
        and receipt.get("finalization_reservation_id") == reservation.reservation_id
    )
    active_attempt = (
        snapshot.state.value == "running"
        and snapshot.attempt_state is not None
        and snapshot.attempt_state.value == "running"
        and snapshot.lease_until is not None
        and snapshot.lease_until > datetime.now(UTC)
        and snapshot.intent.deadline > datetime.now(UTC)
    )
    if (
        attempt_id is None
        or snapshot.generation < 1
        or not (active_attempt or completed_replay)
    ):
        raise AcquisitionApplicationError(
            "BC04 execution has no current importable attempt"
        )
    binding = snapshot.intent.binding
    binding_digest = hashlib.sha256(
        json.dumps(asdict(binding), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if binding.execution_mode == "fixture-only":
        return TrustedExecutionContext(
            tenant_id=tenant_id,
            run_id=run_id,
            request_id=snapshot.intent.request_id,
            attempt_id=attempt_id,
            generation=snapshot.generation,
            source_id=binding.source_id,
            source_revision_id=binding.source_revision,
            bot_id=binding.bot_id,
            build_id=binding.build_id,
            build_manifest_digest="sha256:" + binding.bot_build_manifest_digest,
            runtime_image_digest=binding.oci_digest,
            policy_digest=None,
            maximum_total_response_bytes=0,
            maximum_requests=0,
            release_id=binding.build_id,
            created_at=snapshot.created_at,
            deadline=snapshot.intent.deadline,
            execution_mode=binding.execution_mode,
            binding_digest=binding_digest,
            replay_output_set_id=(
                reservation.output_set_id if completed_replay else None
            ),
            replay_manifest_digest=(
                reservation.manifest_digest if completed_replay else None
            ),
        )
    if binding.execution_mode == "live-authorized":
        return TrustedExecutionContext(
            tenant_id=tenant_id,
            run_id=run_id,
            request_id=snapshot.intent.request_id,
            attempt_id=attempt_id,
            generation=snapshot.generation,
            source_id=binding.source_id,
            source_revision_id=binding.source_revision_id,
            bot_id=None,
            build_id=binding.build_id,
            build_manifest_digest=binding.build_manifest_digest,
            runtime_image_digest=binding.runtime_image_digest,
            policy_digest=binding.policy_digest,
            maximum_total_response_bytes=binding.max_total_response_bytes,
            maximum_requests=binding.max_requests,
            release_id=binding.release_id,
            created_at=snapshot.created_at,
            deadline=snapshot.intent.deadline,
            execution_mode=binding.execution_mode,
            binding_digest=binding_digest,
            replay_output_set_id=(
                reservation.output_set_id if completed_replay else None
            ),
            replay_manifest_digest=(
                reservation.manifest_digest if completed_replay else None
            ),
        )
    raise AcquisitionApplicationError("BC04 execution mode is unsupported")
