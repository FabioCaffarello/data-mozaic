# BC13 Work Intake

This package is the in-process BC13 application boundary for immutable logical
Input bindings, parameter validation, durable Submission intents and recoverable
dispatch to BC04. It does not own CaptureRun, CaptureAttempt, policy admission,
payload output, artifact custody or publication state.

## Current implementation

- `WorkIntake.submit` resolves the exact eligible BotRelease and Input Contract,
  validates bounded canonical parameter JSON, and persists an idempotent intent
  through the BC13 PostgreSQL adapter.
- `WorkIntake.dispatch_pending` claims bounded work and calls BC04 through a
  consumer-owned admission port. A stable operation ID makes retries resolve the
  original Run; BC04 remains authoritative for Run state and cancellation.
- `work_intake.adapters.composition.compose_work_intake` is the reusable in-process
  operational composition. It wires BC13 PostgreSQL and contract validation to
  the existing M4 lifecycle resolver and BC04 coordinator ports. Its
  `WorkIntakeOperations` facade exposes submit, bounded dispatch/reconciliation,
  submission inspection, authoritative execution inspection and cancellation.
  The facade owns no terminal execution state.
- Cancellation before the first dispatch claim is owned by BC13. Once an
  admission attempt has occurred, cancellation first resolves the BC04 receipt;
  a recovered Run is attached to the submission and cancellation is delegated
  to BC04. If no receipt is visible yet, cancellation remains unresolved and
  does not invent a BC13 terminal outcome.
- The current compiled parameter capability supports one declared scalar query
  parameter and one initial GET response. Parameterized compilation rejects
  continuation/pagination operations, and BC04 rejects a second gateway hop
  for that execution. Existing nonparameterized redirect and pagination
  profiles retain their prior behavior.

The package is Python because current BC04/M4 application composition and BC13
ports are Python APIs. The Go acquisition gateway remains the live HTTP and
authorization enforcement service; no Work Intake service/API or broker is
introduced.

## Validation

`make work-intake-tests` runs domain/application tests.
`make work-intake-postgres-tests` applies the owner-scoped migration and runs
the PostgreSQL integration tests against the preserved local database.

After configuring the existing M4 lifecycle and BC04 coordinator applications,
an operator can compose BC13 with:

```python
operations = compose_work_intake(
    database_url=work_intake_database_url,
    migrations_root=repository_root / "migrations",
    contract_root=repository_root / "contracts/payload/draft/v1",
    lifecycle=acquisition_lifecycle,
    coordinator=durable_execution_coordinator,
)
submission = operations.submit(...)
operations.dispatch_pending(worker_id="work-intake-operator", limit=10)
submission = operations.inspect(tenant_id, submission.intent.submission_id)
outcome = operations.inspect_execution(tenant_id, submission.intent.submission_id)
operations.cancel(tenant_id, submission.intent.submission_id)
```

An intentional reacquisition submits the same logical Input with a new
idempotency key, producing a distinct Submission and CaptureRun. Admitted work
is executed through existing BC04 and acquisition application interfaces: the
operator resolves the admitted Run, calls `coordinator.acquire_attempt` and
`coordinator.issue_execution_capability`, starts the protected worker with the
verified release build and capability, then passes its result to
`TrustedAcquisitionApplication.verify_live_result` and
`import_verified_result`. BC04 supplies the durable parameter binding to the
current attempt; the operator does not choose runtime parameters. The importer
uses BC06 custody and the existing BC04 reservation and BC05 publication path.
`make work-intake-acceptance` exercises these real interfaces with PostgreSQL,
MongoDB, RustFS, Scrapy, the Go gateway, BC04, BC05 and BC06. See the [M5
implementation assessment](../../docs/architecture/assessments/work-intake-2026-11-26.md)
for the W1–W18 evidence matrix and current limits.
