"""BC04-owned ports for durable coordination and BC05 finalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from execution_coordination.durable_domain import (
    AttemptLease,
    DurableRunIntent,
    DurableRunSnapshot,
    FailureDisposition,
    FinalizationGrant,
    FinalizationReservation,
    TrustedLiveBinding,
)


class DurableRunRepositoryPort(Protocol):
    def create(self, intent: DurableRunIntent) -> DurableRunSnapshot: ...
    def get(self, tenant_id: str, run_id: str) -> DurableRunSnapshot | None: ...
    def get_by_idempotency(
        self, tenant_id: str, idempotency_key: str
    ) -> DurableRunSnapshot | None: ...
    def claim(
        self,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        operation_id: str,
        lease_seconds: int,
    ) -> AttemptLease: ...
    def admit_acquisition_operation(
        self,
        capability: Mapping[str, object],
        *,
        operation_id: str,
        target_digest: str,
    ) -> Mapping[str, object]: ...
    def renew(
        self, lease: AttemptLease, operation_id: str, lease_seconds: int
    ) -> AttemptLease: ...
    def fail_attempt(
        self,
        lease: AttemptLease,
        disposition: FailureDisposition,
        diagnostic_code: str,
        operation_id: str,
    ) -> DurableRunSnapshot: ...
    def reserve_finalization(
        self, lease: AttemptLease, grant: FinalizationGrant
    ) -> FinalizationReservation: ...
    def complete_finalization(
        self,
        tenant_id: str,
        run_id: str,
        reservation_id: str,
        receipt: Mapping[str, object],
        operation_id: str,
    ) -> DurableRunSnapshot: ...
    def cancel(
        self, tenant_id: str, run_id: str, operation_id: str
    ) -> DurableRunSnapshot: ...
    def expire_leases(self, *, limit: int = 100) -> tuple[DurableRunSnapshot, ...]: ...
    def recover_expired_run(
        self, tenant_id: str, run_id: str
    ) -> DurableRunSnapshot | None: ...
    def list_recoverable(
        self, *, limit: int = 100
    ) -> tuple[DurableRunSnapshot, ...]: ...
    def list_pending_finalizations(
        self,
        *,
        limit: int = 50,
        tenant_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[DurableRunSnapshot, ...]: ...


class PublicationGrantSignerPort(Protocol):
    def issue(self, fields: Mapping[str, object]) -> FinalizationGrant: ...


class PublicationPort(Protocol):
    def publish_grant(self, grant: FinalizationGrant) -> Mapping[str, object]: ...
    def get_publication_receipt(
        self, tenant_id: str, output_set_id: str
    ) -> Mapping[str, object] | None: ...


class LiveAuthorityPort(Protocol):
    def assert_durably_current(self) -> None: ...

    def resolve(
        self,
        *,
        source_id: str,
        source_revision_id: str,
        policy_id: str,
        policy_version: str,
        release_id: str,
        build_id: str,
        build_manifest_digest: str,
        runtime_image_digest: str,
    ) -> TrustedLiveBinding: ...


class AuthoritySnapshotRegistryPort(Protocol):
    def install_authority_snapshot(self, **snapshot: object) -> int: ...

    def authority_snapshot_is_current(
        self, *, authority_id: str, generation: int, snapshot_digest: str
    ) -> bool: ...


class ExecutionCapabilityIssuerPort(Protocol):
    def issue(self, lease: AttemptLease, **claims: object) -> Mapping[str, object]: ...


class ExecutionCapabilityVerifierPort(Protocol):
    def verify(self, capability: Mapping[str, object], *, audience: str) -> None: ...
