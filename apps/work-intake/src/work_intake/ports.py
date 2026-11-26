"""Consumer-owned interfaces for BC13 collaboration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from mozaic_contract_resolution import ContractReference, ResolvedJSONSchema

from work_intake.domain import LogicalInput, Submission, SubmissionIntent


class InputContractResolverPort(Protocol):
    def resolve(self, reference: ContractReference) -> ResolvedJSONSchema: ...


class EligibleReleaseResolverPort(Protocol):
    def resolve_eligible_release(self, release_id: str) -> Mapping[str, Any]: ...


class WorkIntakeStorePort(Protocol):
    def accept(
        self, logical_input: LogicalInput, intent: SubmissionIntent
    ) -> Submission: ...

    def get_by_idempotency(
        self, tenant_id: str, idempotency_scope: str, idempotency_key: str
    ) -> Submission | None: ...

    def get_submission(
        self, tenant_id: str, submission_id: str
    ) -> Submission | None: ...

    def claim_pending(
        self,
        *,
        limit: int,
        worker_id: str,
        lease_seconds: int,
        tenant_id: str | None = None,
    ) -> tuple[Submission, ...]: ...

    def mark_admitted(
        self,
        tenant_id: str,
        submission_id: str,
        worker_id: str,
        claim_generation: int,
        run_id: str,
    ) -> Submission: ...

    def mark_rejected(
        self,
        tenant_id: str,
        submission_id: str,
        worker_id: str,
        claim_generation: int,
        diagnostic_code: str,
    ) -> Submission: ...

    def release_claim(
        self,
        tenant_id: str,
        submission_id: str,
        worker_id: str,
        claim_generation: int,
    ) -> None: ...

    def cancel_pending(self, tenant_id: str, submission_id: str) -> Submission: ...

    def attach_existing_admission(
        self, tenant_id: str, submission_id: str, run_id: str
    ) -> Submission: ...


class CaptureRunAdmissionPort(Protocol):
    """BC04 public admission command; no BC04 persistence is exposed."""

    def admit(self, submission: Submission, release: Mapping[str, Any]) -> str: ...

    def resolve_existing(self, submission: Submission) -> str | None: ...


class CaptureRunOutcomeReaderPort(Protocol):
    """Read Run state from its BC04 owner without creating a BC13 projection."""

    def inspect(self, tenant_id: str, run_id: str) -> Any: ...


class CaptureRunCancellationPort(Protocol):
    """Delegate admitted Run cancellation to BC04."""

    def cancel(self, tenant_id: str, run_id: str, operation_id: str) -> Any: ...
