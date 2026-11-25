"""Durable BC04 use cases and scoped publication-grant issuance."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import rfc8785

from execution_coordination.domain import CoordinationError
from execution_coordination.durable_domain import (
    AttemptLease,
    DurableRunIntent,
    DurableRunSnapshot,
    FailureDisposition,
    FinalizationReservation,
)


class FinalizationRecoveryState(StrEnum):
    COMPLETED = "completed"
    UNRESOLVED = "unresolved"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class FinalizationRecoveryResult:
    tenant_id: str
    run_id: str
    reservation_id: str
    state: FinalizationRecoveryState
    run_state: str
    reason_code: str | None = None


from execution_coordination.durable_ports import (
    DurableRunRepositoryPort,
    PublicationGrantSignerPort,
    PublicationPort,
)


def _canonical_intent(fields: Mapping[str, object]) -> bytes:
    try:
        return rfc8785.dumps(dict(fields))
    except Exception as error:
        raise CoordinationError(
            "finalization identity is not canonical JSON"
        ) from error


class DurableExecutionCoordinator:
    """Command layer for PostgreSQL-owned Runs and BC05-owned publication."""

    def __init__(
        self,
        repository: DurableRunRepositoryPort,
        publication: PublicationPort,
        grant_signer: PublicationGrantSignerPort | None,
        *,
        lease_seconds: int = 30,
    ) -> None:
        if not 1 <= lease_seconds <= 300:
            raise CoordinationError("lease duration is outside the supported range")
        self._repository = repository
        self._publication = publication
        self._grant_signer = grant_signer
        self._lease_seconds = lease_seconds

    def create_run(self, intent: DurableRunIntent) -> DurableRunSnapshot:
        return self._repository.create(intent)

    def inspect(self, tenant_id: str, run_id: str) -> DurableRunSnapshot:
        snapshot = self._repository.get(tenant_id, run_id)
        if snapshot is None:
            raise CoordinationError("CaptureRun does not exist")
        return snapshot

    def recover_expired_run(self, tenant_id: str, run_id: str) -> DurableRunSnapshot:
        """Apply BC04's normal expiry transition to one explicitly scoped run."""
        snapshot = self._repository.recover_expired_run(tenant_id, run_id)
        if snapshot is None:
            raise CoordinationError("CaptureRun does not exist")
        return snapshot

    def acquire_attempt(
        self, tenant_id: str, run_id: str, worker_id: str, operation_id: str
    ) -> AttemptLease:
        return self._repository.claim(
            tenant_id,
            run_id,
            worker_id,
            secrets.token_urlsafe(32),
            operation_id,
            self._lease_seconds,
        )

    def renew_attempt(self, lease: AttemptLease, operation_id: str) -> AttemptLease:
        return self._repository.renew(lease, operation_id, self._lease_seconds)

    def fail_attempt(
        self,
        lease: AttemptLease,
        disposition: FailureDisposition,
        diagnostic_code: str,
        operation_id: str,
    ) -> DurableRunSnapshot:
        return self._repository.fail_attempt(
            lease, disposition, diagnostic_code, operation_id
        )

    def reserve_and_publish(
        self,
        lease: AttemptLease,
        output_set_id: str,
        manifest_digest: str,
        operation_id: str,
    ) -> tuple[FinalizationReservation, Mapping[str, object]]:
        reservation_id = (
            "res-"
            + hashlib.sha256(
                _canonical_intent(
                    {
                        "run_id": lease.run_id,
                        "attempt_id": lease.attempt_id,
                        "generation": lease.generation,
                        "output_set_id": output_set_id,
                        "manifest_digest": manifest_digest,
                    }
                )
            ).hexdigest()
        )
        if self._grant_signer is None:
            raise CoordinationError("publication grant signer is not configured")
        grant = self._grant_signer.issue(
            {
                "tenant_id": lease.tenant_id,
                "run_id": lease.run_id,
                "attempt_id": lease.attempt_id,
                "fencing_generation": lease.generation,
                "output_set_id": output_set_id,
                "manifest_digest": manifest_digest,
                "reservation_id": reservation_id,
                "operation_id": operation_id,
                "issued_at": datetime.now(UTC).isoformat(),
            }
        )
        reservation = self._repository.reserve_finalization(lease, grant)
        receipt = self._publication.publish_grant(reservation.grant)
        self._validate_receipt(lease.tenant_id, lease.run_id, reservation, receipt)
        self._repository.complete_finalization(
            lease.tenant_id,
            lease.run_id,
            reservation.reservation_id,
            receipt,
            "complete-" + reservation.reservation_id,
        )
        return reservation, receipt

    def complete_from_receipt(
        self,
        tenant_id: str,
        run_id: str,
        reservation_id: str,
        receipt: Mapping[str, object],
        operation_id: str,
    ) -> DurableRunSnapshot:
        snapshot = self.inspect(tenant_id, run_id)
        reservation = snapshot.reservation
        if reservation is None or reservation.reservation_id != reservation_id:
            raise CoordinationError("PublicationReceipt has no matching reservation")
        self._validate_receipt(tenant_id, run_id, reservation, receipt)
        return self._repository.complete_finalization(
            tenant_id, run_id, reservation_id, receipt, operation_id
        )

    def cancel(
        self, tenant_id: str, run_id: str, operation_id: str
    ) -> DurableRunSnapshot:
        return self._repository.cancel(tenant_id, run_id, operation_id)

    def reconcile_finalization(self, tenant_id: str, run_id: str) -> DurableRunSnapshot:
        snapshot = self.inspect(tenant_id, run_id)
        reservation = snapshot.reservation
        if reservation is None:
            return snapshot
        receipt = self._publication.get_publication_receipt(
            tenant_id, reservation.output_set_id
        )
        if receipt is None:
            # Re-dispatch the same durable grant. BC05's operation receipt makes
            # this safe after a lost acknowledgment or a coordinator restart.
            receipt = self._publication.publish_grant(reservation.grant)
        self._validate_receipt(tenant_id, run_id, reservation, receipt)
        return self._repository.complete_finalization(
            tenant_id,
            run_id,
            reservation.reservation_id,
            receipt,
            "complete-" + reservation.reservation_id,
        )

    def reconcile(
        self,
        *,
        limit: int = 50,
        timeout_seconds: int = 60,
        tenant_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[FinalizationRecoveryResult, ...]:
        if not 1 <= limit <= 100:
            raise CoordinationError(
                "finalization recovery batch must be from 1 through 100"
            )
        if not 1 <= timeout_seconds <= 300:
            raise CoordinationError(
                "recovery timeout must be from 1 through 300 seconds"
            )
        if (tenant_id is None) != (run_id is None):
            raise CoordinationError("tenant_id and run_id must be supplied together")
        deadline = time.monotonic() + timeout_seconds
        candidates = self._repository.list_pending_finalizations(
            limit=limit, tenant_id=tenant_id, run_id=run_id
        )
        results: list[FinalizationRecoveryResult] = []
        for snapshot in candidates:
            if time.monotonic() >= deadline:
                break
            reservation = snapshot.reservation
            if reservation is None:
                results.append(
                    FinalizationRecoveryResult(
                        snapshot.intent.tenant_id,
                        snapshot.intent.run_id,
                        "",
                        FinalizationRecoveryState.CONFLICT,
                        snapshot.state.value,
                        "pending_run_without_reservation",
                    )
                )
                continue
            try:
                recovered = self.reconcile_finalization(
                    snapshot.intent.tenant_id, snapshot.intent.run_id
                )
            except CoordinationError:
                try:
                    latest = self._repository.get(
                        reservation.tenant_id, reservation.run_id
                    )
                except Exception:  # noqa: BLE001 - keep the outcome unresolved
                    latest = None
                if (
                    latest is not None
                    and latest.state.value == "succeeded"
                    and latest.reservation is not None
                    and latest.reservation.reservation_id == reservation.reservation_id
                ):
                    results.append(
                        FinalizationRecoveryResult(
                            reservation.tenant_id,
                            reservation.run_id,
                            reservation.reservation_id,
                            FinalizationRecoveryState.COMPLETED,
                            latest.state.value,
                        )
                    )
                    continue
                results.append(
                    FinalizationRecoveryResult(
                        reservation.tenant_id,
                        reservation.run_id,
                        reservation.reservation_id,
                        FinalizationRecoveryState.CONFLICT
                        if latest is not None
                        else FinalizationRecoveryState.UNRESOLVED,
                        snapshot.state.value,
                        "reservation_or_receipt_conflict"
                        if latest is not None
                        else "coordinator_store_unavailable",
                    )
                )
            except Exception as error:  # noqa: BLE001 - preserve ambiguous reservations
                try:
                    latest = self._repository.get(
                        reservation.tenant_id, reservation.run_id
                    )
                except Exception:  # noqa: BLE001 - keep the outcome unresolved
                    latest = None
                if (
                    latest is not None
                    and latest.state.value == "succeeded"
                    and latest.reservation is not None
                    and latest.reservation.reservation_id == reservation.reservation_id
                ):
                    results.append(
                        FinalizationRecoveryResult(
                            reservation.tenant_id,
                            reservation.run_id,
                            reservation.reservation_id,
                            FinalizationRecoveryState.COMPLETED,
                            latest.state.value,
                        )
                    )
                    continue
                results.append(
                    FinalizationRecoveryResult(
                        reservation.tenant_id,
                        reservation.run_id,
                        reservation.reservation_id,
                        FinalizationRecoveryState.UNRESOLVED,
                        snapshot.state.value,
                        type(error).__name__,
                    )
                )
            else:
                results.append(
                    FinalizationRecoveryResult(
                        reservation.tenant_id,
                        reservation.run_id,
                        reservation.reservation_id,
                        FinalizationRecoveryState.COMPLETED,
                        recovered.state.value,
                    )
                )
        return tuple(results)

    @staticmethod
    def _validate_receipt(
        tenant_id: str,
        run_id: str,
        reservation: FinalizationReservation,
        receipt: Mapping[str, object],
    ) -> None:
        snapshot_required = {
            "contract_version",
            "tenant_id",
            "output_set_id",
            "manifest_digest",
            "publication_id",
            "published_at",
            "finalization_reservation_id",
        }
        if (
            receipt.get("contract_version") != "publication-receipt/v1"
            or receipt.get("tenant_id") != tenant_id
            or receipt.get("finalization_reservation_id") != reservation.reservation_id
            or receipt.get("output_set_id") != reservation.output_set_id
            or receipt.get("manifest_digest") != reservation.manifest_digest
            or receipt.get("operation_id") != reservation.operation_id
            or not isinstance(receipt.get("publication_id"), str)
            or not isinstance(receipt.get("published_at"), str)
            or not snapshot_required.issubset(receipt)
            or reservation.run_id != run_id
        ):
            raise CoordinationError(
                "BC05 PublicationReceipt does not match the finalization reservation"
            )
