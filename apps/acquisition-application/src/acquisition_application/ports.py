"""Consumer-owned ports for trusted acquisition application composition."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from acquisition_output.domain import ArtifactRef, SourceRevisionRef
from execution_coordination.durable_domain import (
    AttemptLease,
    DurableRunSnapshot,
    FinalizationReservation,
)


class ArtifactCustodyPort(Protocol):
    def store_verified_entity(
        self,
        *,
        artifact: ArtifactRef,
        operation_id: str,
        tenant_id: str,
        source: SourceRevisionRef,
        body: bytes,
        representation: str,
        retention_policy_ref: str,
        evidence_ref: str,
    ) -> ArtifactRef: ...


class ExecutionFinalizationPort(Protocol):
    def reserve_and_publish(
        self,
        lease: AttemptLease,
        output_set_id: str,
        manifest_digest: str,
        operation_id: str,
    ) -> tuple[FinalizationReservation, Mapping[str, object]]: ...

    def reconcile_finalization(
        self, tenant_id: str, run_id: str
    ) -> DurableRunSnapshot: ...
