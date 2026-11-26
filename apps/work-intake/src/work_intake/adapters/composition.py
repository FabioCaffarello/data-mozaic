"""Reusable in-process operational composition for BC13 Work Intake.

The composition wires BC13-owned persistence and validation to the existing
M4 release resolver and BC04 admission/outcome/cancellation APIs. It does not
create or persist an execution terminal state of its own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from acquisition_lifecycle.application import AcquisitionLifecycle
from execution_coordination.durable_application import DurableExecutionCoordinator
from mozaic_contract_resolution import ImmutableContractRegistry

from work_intake.adapters.acquisition_lifecycle import (
    AcquisitionLifecycleReleaseResolver,
)
from work_intake.adapters.bc04_admission import BC04LiveRunAdmission
from work_intake.adapters.postgres import PostgresWorkIntakeStore
from work_intake.application import WorkIntake


class WorkIntakeOperations:
    """Operational facade for submit, dispatch, reconciliation and inspection."""

    def __init__(self, application: WorkIntake):
        self._application = application

    def submit(self, **intent: Any):
        return self._application.submit(**intent)

    def dispatch_pending(self, **claim: Any):
        return self._application.dispatch_pending(**claim)

    def reconcile(
        self, *, worker_id: str, limit: int = 10, tenant_id: str | None = None
    ):
        """Retry pending/expired claims; BC04 receipts resolve prior admissions."""
        return self._application.dispatch_pending(
            worker_id=worker_id, limit=limit, tenant_id=tenant_id
        )

    def inspect(self, tenant_id: str, submission_id: str):
        return self._application.inspect(tenant_id, submission_id)

    def inspect_execution(self, tenant_id: str, submission_id: str):
        return self._application.inspect_execution(tenant_id, submission_id)

    def cancel(self, tenant_id: str, submission_id: str):
        return self._application.cancel(tenant_id, submission_id)


def compose_work_intake(
    *,
    database_url: str,
    migrations_root: Path,
    contract_root: Path,
    lifecycle: AcquisitionLifecycle,
    coordinator: DurableExecutionCoordinator,
) -> WorkIntakeOperations:
    """Build BC13 operations over the established M4 and BC04 applications."""
    store = PostgresWorkIntakeStore(database_url, migrations_root)
    contracts = ImmutableContractRegistry(contract_root)
    application = WorkIntake(
        store=store,
        contracts=contracts,
        releases=AcquisitionLifecycleReleaseResolver(lifecycle),
        admission=BC04LiveRunAdmission(coordinator),
        outcomes=coordinator,
        cancellation=coordinator,
    )
    return WorkIntakeOperations(application)
