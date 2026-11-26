"""BC13 submission and recoverable dispatch use cases."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from mozaic_contract_resolution import ContractReference, ContractResolutionError

from work_intake.domain import (
    DispatchRejected,
    DispatchState,
    LogicalInput,
    Submission,
    SubmissionIntent,
    WorkIntakeError,
    canonical_parameters,
)
from work_intake.ports import (
    CaptureRunAdmissionPort,
    CaptureRunCancellationPort,
    CaptureRunOutcomeReaderPort,
    EligibleReleaseResolverPort,
    InputContractResolverPort,
    WorkIntakeStorePort,
)


class WorkIntake:
    """Accept immutable input intent and dispatch it idempotently through BC04."""

    _ADMISSION_AGE = timedelta(minutes=5)

    def __init__(
        self,
        store: WorkIntakeStorePort,
        contracts: InputContractResolverPort,
        releases: EligibleReleaseResolverPort,
        admission: CaptureRunAdmissionPort,
        outcomes: CaptureRunOutcomeReaderPort | None = None,
        cancellation: CaptureRunCancellationPort | None = None,
    ) -> None:
        self._store = store
        self._contracts = contracts
        self._releases = releases
        self._admission = admission
        self._outcomes = outcomes
        self._cancellation = cancellation

    def submit(
        self,
        *,
        logical_input: LogicalInput,
        idempotency_scope: str,
        idempotency_key: str,
        release_id: str,
        parameters: dict[str, Any],
    ) -> Submission:
        """Resolve once, validate, then atomically persist input and dispatch intent."""
        encoded, digest = canonical_parameters(parameters)
        identity_seed = f"data-mozaic:bc13:{logical_input.tenant_id}:{idempotency_scope}:{idempotency_key}"
        submission_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity_seed))
        dispatch_digest = hashlib.sha256(identity_seed.encode()).hexdigest()
        now = datetime.now(UTC)
        intent = SubmissionIntent(
            tenant_id=logical_input.tenant_id,
            submission_id=submission_id,
            input_id=logical_input.input_id,
            source_id=logical_input.source_id,
            source_revision_id=logical_input.source_revision_id,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            release_id=release_id,
            input_contract=logical_input.contract,
            parameters_json=encoded,
            parameters_digest=digest,
            created_at=now,
            dispatch_operation_id="dispatch-" + dispatch_digest,
        )
        existing = self._store.get_by_idempotency(
            logical_input.tenant_id, idempotency_scope, idempotency_key
        )
        if existing is not None:
            if existing.intent.intent_digest() != intent.intent_digest():
                raise WorkIntakeError(
                    "idempotency key was reused with conflicting intent"
                )
            return existing
        try:
            release = self._releases.resolve_eligible_release(release_id)
        except DispatchRejected as error:
            raise WorkIntakeError(error.diagnostic_code) from error
        if (
            release.get("release_id") != release_id
            or logical_input.contract != self._release_input_contract(release)
            or release.get("source_id") != logical_input.source_id
            or release.get("source_revision_id") != logical_input.source_revision_id
        ):
            raise WorkIntakeError("input binding differs from the approved release")
        try:
            schema = self._contracts.resolve(logical_input.contract)
        except ContractResolutionError as error:
            raise WorkIntakeError("input contract cannot be resolved") from error
        schema.validate(parameters)
        return self._store.accept(logical_input, intent)

    def dispatch_pending(
        self,
        *,
        worker_id: str,
        limit: int = 10,
        lease_seconds: int = 30,
        tenant_id: str | None = None,
    ) -> tuple[Submission, ...]:
        if not 1 <= limit <= 100 or not 1 <= lease_seconds <= 300:
            raise WorkIntakeError("dispatch claim bounds are invalid")
        claimed = self._store.claim_pending(
            limit=limit,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            tenant_id=tenant_id,
        )
        outcomes: list[Submission] = []
        for submission in claimed:
            try:
                # A prior call may have committed in BC04 while its acknowledgment
                # was lost. Resolve that stable operation before age or eligibility
                # checks, which may have changed since the original admission.
                existing_run_id = self._admission.resolve_existing(submission)
                if existing_run_id is not None:
                    outcomes.append(
                        self._store.mark_admitted(
                            submission.intent.tenant_id,
                            submission.intent.submission_id,
                            worker_id,
                            submission.claim_generation,
                            existing_run_id,
                        )
                    )
                    continue
                if (
                    datetime.now(UTC)
                    >= submission.intent.created_at + self._ADMISSION_AGE
                ):
                    raise DispatchRejected("submission_expired")
                release = self._releases.resolve_eligible_release(
                    submission.intent.release_id
                )
                if not _release_matches_submission(release, submission):
                    raise DispatchRejected("release_binding_mismatch")
                run_id = self._admission.admit(submission, release)
                outcomes.append(
                    self._store.mark_admitted(
                        submission.intent.tenant_id,
                        submission.intent.submission_id,
                        worker_id,
                        submission.claim_generation,
                        run_id,
                    )
                )
            except DispatchRejected as error:
                outcomes.append(
                    self._store.mark_rejected(
                        submission.intent.tenant_id,
                        submission.intent.submission_id,
                        worker_id,
                        submission.claim_generation,
                        error.diagnostic_code,
                    )
                )
            except Exception:
                # An unavailable or ambiguous external admission is not a
                # definitive failure. Release the claim; retry reuses the same
                # durable dispatch identity and BC04 idempotency key.
                self._store.release_claim(
                    submission.intent.tenant_id,
                    submission.intent.submission_id,
                    worker_id,
                    submission.claim_generation,
                )
                raise
        return tuple(outcomes)

    def inspect(self, tenant_id: str, submission_id: str) -> Submission:
        result = self._store.get_submission(tenant_id, submission_id)
        if result is None:
            raise WorkIntakeError("submission does not exist")
        return result

    def inspect_execution(self, tenant_id: str, submission_id: str) -> Any:
        """Read the authoritative Run state from BC04 for an admitted submission."""
        if self._outcomes is None:
            raise WorkIntakeError("BC04 outcome reader is not configured")
        submission = self.inspect(tenant_id, submission_id)
        if submission.run_id is None:
            raise WorkIntakeError("submission has no admitted CaptureRun")
        return self._outcomes.inspect(tenant_id, submission.run_id)

    def cancel(self, tenant_id: str, submission_id: str) -> Submission | Any:
        """Cancel pending BC13 work or delegate an admitted Run to BC04."""
        submission = self.inspect(tenant_id, submission_id)
        if submission.state == DispatchState.PENDING:
            if submission.claim_generation == 0:
                return self._store.cancel_pending(tenant_id, submission_id)
            if self._admission is None:
                raise WorkIntakeError(
                    "previous dispatch outcome must be reconciled before cancellation"
                )
            existing_run_id = self._admission.resolve_existing(submission)
            if existing_run_id is None:
                raise WorkIntakeError(
                    "previous dispatch outcome is unresolved; retry cancellation after reconciliation"
                )
            submission = self._store.attach_existing_admission(
                tenant_id, submission_id, existing_run_id
            )
        if submission.state == DispatchState.CLAIMED:
            raise WorkIntakeError("dispatch is in progress; cancellation is unresolved")
        if submission.state == DispatchState.ADMITTED:
            if self._cancellation is None or submission.run_id is None:
                raise WorkIntakeError("BC04 cancellation is not configured")
            operation_id = (
                "cancel-"
                + hashlib.sha256(
                    f"{tenant_id}:{submission.intent.submission_id}".encode()
                ).hexdigest()
            )
            return self._cancellation.cancel(tenant_id, submission.run_id, operation_id)
        return submission

    def _release_input_contract(self, release: Any) -> ContractReference:
        raw = release.get("input_contract")
        if not isinstance(raw, dict) or set(raw) != {
            "contract_id",
            "version",
            "digest",
        }:
            raise WorkIntakeError("release has no immutable input contract binding")
        try:
            return ContractReference(raw["contract_id"], raw["version"], raw["digest"])
        except (TypeError, ContractResolutionError) as error:
            raise WorkIntakeError(
                "release input contract binding is invalid"
            ) from error


def _release_matches_submission(release: Any, submission: Submission) -> bool:
    contract = release.get("input_contract")
    expected = submission.intent.input_contract
    return (
        release.get("release_id") == submission.intent.release_id
        and release.get("status") == "approved"
        and release.get("contract_version") == "bot-release/v3"
        and release.get("source_id") == submission.intent.source_id
        and release.get("source_revision_id") == submission.intent.source_revision_id
        and isinstance(contract, dict)
        and contract.get("contract_id") == expected.contract_id
        and contract.get("version") == expected.version
        and contract.get("digest") == expected.digest
    )
