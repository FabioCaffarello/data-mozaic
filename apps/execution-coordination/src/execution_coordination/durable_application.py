"""Durable BC04 use cases and scoped publication-grant issuance."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlencode, urlsplit, urlunsplit

import rfc8785

from execution_coordination.domain import CoordinationError
from execution_coordination.durable_domain import (
    AttemptLease,
    DurableRunIntent,
    DurableRunSnapshot,
    FailureDisposition,
    FinalizationReservation,
    LiveExecutionBinding,
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
    ExecutionCapabilityIssuerPort,
    ExecutionCapabilityVerifierPort,
    LiveAuthorityPort,
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
        live_authority: LiveAuthorityPort | None = None,
        execution_capability_signer: ExecutionCapabilityIssuerPort | None = None,
        execution_capability_verifier: ExecutionCapabilityVerifierPort | None = None,
    ) -> None:
        if not 1 <= lease_seconds <= 300:
            raise CoordinationError("lease duration is outside the supported range")
        self._repository = repository
        self._publication = publication
        self._grant_signer = grant_signer
        self._lease_seconds = lease_seconds
        self._live_authority = live_authority
        self._execution_capability_signer = execution_capability_signer
        self._execution_capability_verifier = execution_capability_verifier

    def create_run(self, intent: DurableRunIntent) -> DurableRunSnapshot:
        if intent.binding.execution_mode != "fixture-only":
            raise CoordinationError("live runs require trusted live admission")
        return self._repository.create(intent)

    def admit_live_run(
        self,
        *,
        tenant_id: str,
        run_id: str,
        request_id: str,
        idempotency_key: str,
        deadline: datetime,
        source_id: str,
        source_revision_id: str,
        policy_id: str,
        policy_version: str,
        release_id: str,
        build_id: str,
        build_manifest_digest: str,
        runtime_image_digest: str,
        input_id: str | None = None,
        submission_id: str | None = None,
        input_parameters: Mapping[str, object] | None = None,
        input_contract: Mapping[str, str] | None = None,
        max_attempts: int = 3,
    ) -> DurableRunSnapshot:
        """Resolve all live authorization from the trusted operator snapshot."""
        if self._live_authority is None:
            raise CoordinationError("trusted live authority is not configured")
        self._live_authority.assert_durably_current()
        resolved = self._live_authority.resolve(
            source_id=source_id,
            source_revision_id=source_revision_id,
            policy_id=policy_id,
            policy_version=policy_version,
            release_id=release_id,
            build_id=build_id,
            build_manifest_digest=build_manifest_digest,
            runtime_image_digest=runtime_image_digest,
        )
        expected_initial_target_digest = None
        trusted_input_contract = resolved.release.get("input_contract")
        if isinstance(trusted_input_contract, dict):
            if (
                not input_id
                or not submission_id
                or not isinstance(input_parameters, Mapping)
                or not isinstance(input_contract, Mapping)
                or dict(input_contract)
                != {
                    "contract_id": trusted_input_contract.get("contract_id"),
                    "version": trusted_input_contract.get("version"),
                    "digest": trusted_input_contract.get("digest"),
                }
            ):
                raise CoordinationError(
                    "BotRelease v3 requires an immutable Work Intake binding"
                )
            try:
                input_parameters_json = rfc8785.dumps(dict(input_parameters)).decode(
                    "utf-8"
                )
            except Exception as error:
                raise CoordinationError(
                    "input parameters are not canonical JSON"
                ) from error
            if len(input_parameters_json.encode("utf-8")) > 64_000:
                raise CoordinationError("input parameters exceed the supported size")
            input_parameters_digest = (
                "sha256:"
                + hashlib.sha256(input_parameters_json.encode("utf-8")).hexdigest()
            )
            input_request = resolved.release.get("input_request")
            input_query = (
                input_request.get("input_query")
                if isinstance(input_request, Mapping)
                else None
            )
            parameter_name = (
                input_query.get("parameter")
                if isinstance(input_query, Mapping)
                else None
            )
            query_name = (
                input_query.get("query_name")
                if isinstance(input_query, Mapping)
                else None
            )
            parameterless = (
                isinstance(input_request, Mapping)
                and resolved.release.get("contract_version") == "bot-release/v4"
                and set(input_request) == {"method", "url"}
                and input_query is None
                and not input_parameters
            )
            if (
                not isinstance(input_request, Mapping)
                or input_request.get("method") != "GET"
                or (
                    not parameterless
                    and (
                        set(input_request) != {"method", "url", "input_query"}
                        or not isinstance(parameter_name, str)
                        or not isinstance(query_name, str)
                        or set(input_parameters) != {parameter_name}
                        or isinstance(input_parameters[parameter_name], bool)
                        or not isinstance(input_parameters[parameter_name], (int, str))
                    )
                )
            ):
                raise CoordinationError("BotRelease input request binding is invalid")
            try:
                base = urlsplit(str(input_request.get("url", "")))
                if (
                    base.scheme != "https"
                    or not base.netloc
                    or base.username is not None
                    or base.password is not None
                    or base.query
                    or base.fragment
                ):
                    raise ValueError("unsafe base URL")
                initial_url = (
                    base.geturl()
                    if parameterless
                    else urlunsplit(
                        (
                            base.scheme,
                            base.netloc,
                            base.path,
                            urlencode({query_name: input_parameters[parameter_name]}),
                            "",
                        )
                    )
                )
            except (TypeError, ValueError) as error:
                raise CoordinationError(
                    "BotRelease input request URL is invalid"
                ) from error
            expected_initial_target_digest = (
                "sha256:" + hashlib.sha256(initial_url.encode("utf-8")).hexdigest()
            )
        elif any(
            value is not None
            for value in (
                input_id,
                submission_id,
                input_parameters,
                input_contract,
            )
        ):
            raise CoordinationError("BotRelease v2 cannot bind Work Intake parameters")
        else:
            input_parameters_json = None
            input_parameters_digest = None
            expected_initial_target_digest = None
        policy = resolved.policy
        output_contract = resolved.release.get("output_contract")
        artifact_mode = resolved.release.get("contract_version") == "bot-release/v4"
        if artifact_mode and (
            not isinstance(output_contract, Mapping)
            or output_contract.get("mode") != "artifact"
            or not isinstance(output_contract.get("media_types"), list)
            or not isinstance(output_contract.get("max_bytes"), int)
        ):
            raise CoordinationError("BotRelease artifact output binding is invalid")
        payload_contract = resolved.release.get("payload_contract")
        if not artifact_mode and not isinstance(payload_contract, Mapping):
            raise CoordinationError("BotRelease payload contract binding is invalid")
        binding = LiveExecutionBinding(
            source_id=source_id,
            source_revision_id=source_revision_id,
            policy_id=policy_id,
            policy_version=policy_version,
            policy_digest=str(policy["policy_digest"]),
            release_id=release_id,
            build_id=build_id.removeprefix("sha256:"),
            build_manifest_digest=build_manifest_digest,
            runtime_profile_id=str(resolved.release["runtime_profile_id"]),
            runtime_image_digest=runtime_image_digest,
            authority_id=resolved.authority_id,
            authority_snapshot_generation=resolved.snapshot_generation,
            authority_snapshot_digest=resolved.snapshot_digest,
            max_duration_seconds=int(policy["deadline_seconds"]),
            max_requests=1 if artifact_mode else int(policy["max_requests"]),
            max_total_response_bytes=(
                min(
                    int(policy["max_total_response_bytes"]),
                    int(output_contract["max_bytes"]),
                )
                if artifact_mode
                else int(policy["max_total_response_bytes"])
            ),
            max_concurrency=int(policy["max_concurrency"]),
            payload_contract_id=(
                str(payload_contract["contract_id"])
                if isinstance(payload_contract, Mapping)
                else None
            ),
            payload_contract_version=(
                str(payload_contract["version"])
                if isinstance(payload_contract, Mapping)
                else None
            ),
            payload_contract_digest=(
                str(payload_contract["digest"])
                if isinstance(payload_contract, Mapping)
                else None
            ),
            input_contract_id=(
                str(resolved.release["input_contract"]["contract_id"])
                if isinstance(trusted_input_contract, dict)
                else None
            ),
            input_contract_version=(
                str(resolved.release["input_contract"]["version"])
                if isinstance(trusted_input_contract, dict)
                else None
            ),
            input_contract_digest=(
                str(resolved.release["input_contract"]["digest"])
                if isinstance(trusted_input_contract, dict)
                else None
            ),
            input_id=input_id,
            submission_id=submission_id,
            input_parameters_json=input_parameters_json,
            input_parameters_digest=input_parameters_digest,
            expected_initial_target_digest=expected_initial_target_digest,
            output_modality="artifact" if artifact_mode else "record",
            artifact_media_types=tuple(output_contract["media_types"])
            if artifact_mode
            else (),
            max_artifact_bytes=int(output_contract["max_bytes"])
            if artifact_mode
            else None,
        )
        return self._repository.create(
            DurableRunIntent(
                tenant_id=tenant_id,
                run_id=run_id,
                request_id=request_id,
                idempotency_key=idempotency_key,
                binding=binding,
                deadline=deadline,
                max_attempts=max_attempts,
            )
        )

    def inspect(self, tenant_id: str, run_id: str) -> DurableRunSnapshot:
        snapshot = self._repository.get(tenant_id, run_id)
        if snapshot is None:
            raise CoordinationError("CaptureRun does not exist")
        return snapshot

    def resolve_admission(
        self, tenant_id: str, idempotency_key: str
    ) -> DurableRunSnapshot | None:
        """Resolve an earlier idempotent admission after an ambiguous acknowledgment."""
        return self._repository.get_by_idempotency(tenant_id, idempotency_key)

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

    def issue_execution_capability(
        self,
        lease: AttemptLease,
        *,
        audience: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Issue only while the exact durable attempt lease remains current."""
        if self._execution_capability_signer is None:
            raise CoordinationError("execution capability signer is not configured")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        snapshot = self.inspect(lease.tenant_id, lease.run_id)
        binding = snapshot.intent.binding
        if not isinstance(binding, LiveExecutionBinding):
            raise CoordinationError("fixture-only runs cannot receive live capability")
        if (
            snapshot.state.value != "running"
            or snapshot.attempt_state is None
            or snapshot.attempt_state.value != "running"
            or snapshot.attempt_id != lease.attempt_id
            or snapshot.generation != lease.generation
            or snapshot.lease_until != lease.lease_until
            or snapshot.lease_until is None
            or snapshot.lease_until <= current
            or lease.attempt_deadline <= current
        ):
            raise CoordinationError(
                "execution capability requires the current attempt lease"
            )
        return self._execution_capability_signer.issue(
            lease,
            source_id=binding.source_id,
            source_revision_id=binding.source_revision_id,
            policy_id=binding.policy_id,
            policy_version=binding.policy_version,
            policy_digest=binding.policy_digest,
            release_id=binding.release_id,
            build_id="sha256:" + binding.build_id,
            build_manifest_digest=binding.build_manifest_digest,
            runtime_profile_id=binding.runtime_profile_id,
            input_parameters_digest=binding.input_parameters_digest,
            expected_initial_target_digest=binding.expected_initial_target_digest,
            audience=audience,
            execution_deadline=min(snapshot.intent.deadline, lease.attempt_deadline),
            now=current,
        )

    def authorize_acquisition_operation(
        self,
        capability: Mapping[str, object],
        *,
        operation_id: str,
        target_digest: str,
        audience: str = "data-mozaic/acquisition-gateway",
    ) -> Mapping[str, object]:
        """Durably admit one gateway operation at BC04's current-attempt lock point."""
        if self._execution_capability_verifier is None:
            raise CoordinationError("execution capability verifier is not configured")
        self._execution_capability_verifier.verify(capability, audience=audience)
        return self._repository.admit_acquisition_operation(
            capability, operation_id=operation_id, target_digest=target_digest
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
