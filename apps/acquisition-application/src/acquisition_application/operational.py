"""Operator-triggered composition across existing acquisition owner APIs.

This module owns no run, attempt, submission, publication, or custody state. It
coordinates the BC13 facade, BC04 durable coordinator, a configured protected
attempt adapter, and the trusted importer supplied by the application root.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class WorkIntakeOperationsPort(Protocol):
    def submit(self, **intent: Any): ...
    def dispatch_pending(self, **claim: Any): ...
    def reconcile(self, **claim: Any): ...
    def inspect(self, tenant_id: str, submission_id: str): ...
    def inspect_execution(self, tenant_id: str, submission_id: str): ...
    def cancel(self, tenant_id: str, submission_id: str): ...


class ProtectedAttemptPort(Protocol):
    """Run one current BC04 attempt in the configured trusted worker adapter."""

    def execute(
        self,
        *,
        submission: object,
        run: object,
        lease: object,
        capability: Mapping[str, object],
    ) -> object: ...


class TrustedAttemptImportPort(Protocol):
    """Verify worker results and import through the existing BC04/BC05/BC06 path."""

    def import_attempt(
        self, *, submission: object, run: object, lease: object, result: object
    ): ...


class ExecutionCoordinationPort(Protocol):
    """BC04 capabilities required by this composition, supplied by its root."""

    def inspect(self, tenant_id: str, run_id: str): ...

    def acquire_attempt(
        self, tenant_id: str, run_id: str, worker_id: str, operation_id: str
    ): ...

    def issue_execution_capability(self, lease: object, *, audience: str): ...

    def reconcile_finalization(self, tenant_id: str, run_id: str): ...


class OperationalAcquisition:
    """Reusable, bounded operator workflow over BC13 and BC04 public APIs.

    The caller configures the trusted worker adapter and trusted importer at the
    application composition root. The operator cannot pass an admission receipt,
    lease, execution capability, or successful terminal state.
    """

    def __init__(
        self,
        *,
        work_intake: WorkIntakeOperationsPort,
        coordinator: ExecutionCoordinationPort,
        protected_attempt: ProtectedAttemptPort,
        trusted_import: TrustedAttemptImportPort,
        audience: str = "data-mozaic/acquisition-gateway",
    ) -> None:
        if not audience or len(audience) > 256:
            raise ValueError("protected worker audience is invalid")
        self._work_intake = work_intake
        self._coordinator = coordinator
        self._protected_attempt = protected_attempt
        self._trusted_import = trusted_import
        self._audience = audience

    def submit(self, **intent: Any):
        return self._work_intake.submit(**intent)

    def dispatch(
        self, *, worker_id: str, limit: int = 10, tenant_id: str | None = None
    ):
        return self._work_intake.dispatch_pending(
            worker_id=worker_id, limit=limit, tenant_id=tenant_id
        )

    def inspect(self, tenant_id: str, submission_id: str):
        submission = self._work_intake.inspect(tenant_id, submission_id)
        execution = self._work_intake.inspect_execution(tenant_id, submission_id)
        return {"submission": submission, "execution": execution}

    def cancel(self, tenant_id: str, submission_id: str):
        return self._work_intake.cancel(tenant_id, submission_id)

    def reconcile(
        self, *, worker_id: str, limit: int = 10, tenant_id: str | None = None
    ):
        return self._work_intake.reconcile(
            worker_id=worker_id, limit=limit, tenant_id=tenant_id
        )

    def execute(self, *, tenant_id: str, submission_id: str, worker_id: str):
        submission = self._work_intake.inspect(tenant_id, submission_id)
        if submission.intent.tenant_id != tenant_id:
            raise ValueError("Submission tenant differs from the operator scope")
        if submission.run_id is None or submission.state.value != "admitted":
            raise ValueError("Submission has no BC04-admitted CaptureRun")
        run_id = submission.run_id
        execution = self._work_intake.inspect_execution(tenant_id, submission_id)
        if execution.intent.run_id != run_id:
            raise ValueError("BC13 and BC04 admission identities differ")
        if execution.state.value == "succeeded":
            return {"state": "succeeded", "replayed": True, "run": execution}
        if execution.state.value in {"failed", "cancelled", "timed_out"}:
            raise ValueError("BC04 run is terminal and cannot be executed again")
        run = self._coordinator.inspect(tenant_id, run_id)

        lease = self._coordinator.acquire_attempt(
            tenant_id,
            run_id,
            worker_id,
            "operator-attempt-" + submission_id,
        )
        capability = self._coordinator.issue_execution_capability(
            lease, audience=self._audience
        )
        result = self._protected_attempt.execute(
            submission=submission,
            run=run,
            lease=lease,
            capability=capability,
        )
        try:
            imported = self._trusted_import.import_attempt(
                submission=submission, run=run, lease=lease, result=result
            )
        except Exception:
            # Only reconcile an existing BC04 reservation. This does not invent
            # a new publication, retry policy, or terminal outcome.
            current = self._coordinator.inspect(tenant_id, run_id)
            if current.reservation is not None:
                self._coordinator.reconcile_finalization(tenant_id, run_id)
            raise
        finally:
            cleanup = getattr(self._protected_attempt, "cleanup", None)
            if callable(cleanup):
                cleanup(result)
        outcome = self._coordinator.inspect(tenant_id, run_id)
        if outcome.state.value != "succeeded" or outcome.publication_receipt is None:
            raise RuntimeError("trusted import did not produce reconciled BC04 success")
        return {
            "state": "succeeded",
            "replayed": False,
            "run": outcome,
            "import": imported,
        }
