# BC04 durable coordination — branch implementation evidence

**State:** implementation on `feat/execution-coordination-durable`; not committed or integrated. ADR 024 is Proposed and ADRs 021–023 retain their existing status.

## Ownership and implementation

BC04 remains Python and owns Run/attempt lifecycle, lease tokens and generations, deadline decisions, finalization reservations, command receipts and its PostgreSQL outbox. BC05 remains owner of CaptureOutput, OutputSet sealing/publication and MongoDB receipts/outbox. BC06 remains owner of artifact custody and RustFS-backed bytes. The current worker execution mode remains fixture-only.

The BC04 PostgreSQL schema is namespaced as `execution_coordination` and uses `bc04_owner` migrations plus a narrowly granted `bc04_app` role in the local setup. The repository performs short transactions, uses PostgreSQL time for lease/deadline checks and locks the Run row for commands that race. A unique reservation binds one Run to one attempt generation, tenant, OutputSet, manifest digest and publication operation. An unresolved reservation prevents attempt replacement.

At the initial implementation snapshot, draft `publication-grant/v1` used HMAC-SHA256. That trust model was superseded by the review amendment at the end of this assessment. The grant scope, durable reservation, receipt binding and BC05 transaction ownership described here remain applicable.

## Cancellation and recovery semantics

- Cancellation that serializes before the finalization reservation makes the Run terminal and causes later reservation to fail.
- Cancellation after reservation records `cancel_requested_at` and does not revoke the grant. A matching publication receipt completes the Run successfully.
- Lease or Run deadline expiry before reservation rejects a stale attempt. The run deadline is also included in bounded recovery selection.
- A BC05 commit with a lost acknowledgment leaves BC04 `finalization_pending`. Reconciliation queries the known OutputSet receipt and re-dispatches the same grant if no receipt exists; stable BC05 operation identity returns the prior publication fact.
- An unresolved reservation has no automatic abort/expiry. It favors safety over availability and can block attempt replacement indefinitely if BC05 is unavailable or the remote result remains ambiguous.

The linearization points are separate: PostgreSQL reservation authorizes one exact publication; MongoDB transaction publishes the OutputSet; PostgreSQL completion records terminal Run success only after matching the BC05 receipt. There is no distributed transaction, exactly-once delivery, multi-OutputSet atomic completion, production service authentication, BC11 authorization, HA or live worker networking.

## Validation run on 2026-11-25

All examples use synthetic fixture values. Existing local PostgreSQL, MongoDB replica-set and BC06/RustFS development services were used; persistent volumes were preserved.

- `make execution-coordination-durable-tests`: passed, 7 real PostgreSQL tests. Coverage includes idempotency and changed intent, independent concurrent attempt acquisition, lease expiry/new generation, cancel-before-reservation and concurrent cancel/reservation, and cancellation after reservation followed by matching receipt completion.
- `uv run --locked --package mozaic-execution-coordination python -m unittest discover -s apps/execution-coordination/tests -v`: passed, 34 tests including existing fixture/runtime tests and the then-current draft grant schema/HMAC vector checks.
- `uv run --locked --package mozaic-acquisition-output python -m unittest discover -s apps/acquisition-output/tests -v`: passed, 47 tests including existing BC05 tests and verifier contract tests.
- `uv run --locked --all-packages python -m unittest discover -s apps/acquisition-output/integration_tests -v` with the preserved MongoDB environment: passed, 17 real replica-set transaction tests, including Run-bound publication denial without a grant and stale grant projection rejection.
- `make execution-coordination-finalization-tests`: passed. It reruns 7 PostgreSQL tests, probes/provisions the existing MongoDB replica set and exercises one synthetic artifact through BC06 HTTP/RustFS, BC05 custody verification and MongoDB publication. An injected timeout occurs after MongoDB publication commits. Fresh MongoDB and BC04 clients recover the receipt, mark the Run succeeded, query the published Output and download/verify artifact bytes. Three pending tenant events were visible at the end of that fixture run.
- `make check`: passed. This includes lock, lint, formatting, documentation, migration, naming, architecture checks, Go vet/tests, existing Python package suites and root tests: 207 root tests passed, 26 environment-gated tests skipped because their dedicated Docker profiles were not invoked.
- `git diff --check`: passed.

The local replica-set/container startup target was deliberately not rerun from this new worktree after Compose recreated and stopped the existing MongoDB container when invoked from a different worktree. The preserved MongoDB worktree's own `make acquisition-output-mongo-up` restored it; all later database checks connected to that existing service without Compose recreation. The BC06 service and persistent volumes remained intact.

## Review boundary

This is local integration evidence, not production readiness. The outbound BC04→BC05 adapter is a local composition, not a production authenticated service endpoint. Operational handling of permanently unresolved reservations must be reviewed before production. Do not promote ADR 024 without the required human review.

## Review amendment — 2026-11-24

The targeted review found that HMAC gave the BC05 verifier enough shared material to mint grants, weakening BC04's issuer ownership. The draft now uses Ed25519: BC04 signs with a private seed and BC05 verifies with public keys only. The signed contract includes the algorithm, key ID and complete publication scope in RFC 8785 canonical JSON. BC05's verifier exposes no signing operation. The local synthetic composition derives its public key from a fixture seed in the trusted test process; production composition must inject only a public-key ring.

Key rotation uses a new key ID for new grants and retains old public keys while reservations signed by those keys remain unresolved. Grants have no clock-based expiry, and unresolved reservations are not deleted. The bounded `DurableExecutionCoordinator.reconcile` use case discovers only pending reservations, retrieves the BC05 receipt or replays the same stored grant/operation, validates exact reservation binding and conditionally records run success. It returns completed, unresolved and conflict classifications. The one-shot operator command invokes this same application use case. Concurrent reconcilers rely on BC05 operation idempotency and BC04 conditional completion rather than process-local locks.

Run-bound OutputSets remain blocked without a verified grant through BC05's application and Mongo adapter paths. Unbound acquisition OutputSets may publish without BC04 because they have no run/attempt binding; this is not an execution completion path. The current local API is an in-process Python composition, not an authenticated production HTTP boundary. Ed25519 prevents a BC05 verifier configured only with public keys from issuing valid BC04 grants, but does not itself authenticate callers or establish BC11 production authorization.

## Delivery-gate validation amendment — 2026-11-25 UTC

The initial validation bullets above are preserved as the implementation snapshot. After review, the signer and verifier were changed from shared HMAC to Ed25519, operational bounded reconciliation was added, and the finalization harness was extended to use two concurrent independent reconcilers and to replay publication through a fresh coordinator and fresh MongoDB client.

Final local evidence on this branch:

- `uv run --locked --package mozaic-execution-coordination python -m unittest discover -s apps/execution-coordination/tests -v`: 34 passed.
- `uv run --locked --package mozaic-acquisition-output python -m unittest discover -s apps/acquisition-output/tests -v`: 49 passed.
- `make execution-coordination-finalization-tests BC06_ENV_FILE=../data-mozaic-artifact-custody/.env.artifact-custody`: 9 real PostgreSQL integration tests passed; the synthetic BC04/PostgreSQL → BC05/MongoDB → BC06/RustFS flow passed, including two concurrent reconciliation clients, receipt recovery after injected post-commit acknowledgment loss, restart reads, artifact byte/digest verification and publication replay from fresh clients.
- BC05 MongoDB replica-set verification from this branch (with the replica set started from its owning MongoDB worktree): ping passed, schema version 2 provisioning/drift check passed, and 17 integration tests passed.
- `make check`: passed; 207 root tests passed and 26 environment-gated tests skipped.
- `actionlint .github/workflows/ci.yml`: passed. The workflow now runs `make execution-coordination-finalization-tests` in the real PostgreSQL/MongoDB/RustFS integration job. This local workflow edit has not yet run as remote CI evidence.
- `git diff --check`: passed.

The first attempt to invoke `make acquisition-output-mongo-tests` from the BC04 worktree failed because that target's Compose bind mount expected `.local/mongodb/keyfile` in that worktree; the ignored keyfile exists only in the MongoDB owner worktree. Compose stopped the container before tests ran and did not remove or alter the named data volume. The MongoDB owner worktree's ordinary `make acquisition-output-mongo-up` restored the service. Ping, provisioning and all 17 BC05 integration tests then passed against the existing volume. Do not launch the MongoDB Compose target from a worktree without its own local keyfile.

Remote CI, human review, branch protection and PR integration remain outstanding until the authorized BC04 PR is published. This local evidence does not establish production authentication, distributed fencing beyond the durable reservation protocol, exactly-once delivery, production HA or live acquisition readiness.
