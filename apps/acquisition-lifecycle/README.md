# Acquisition Lifecycle application

`mozaic-acquisition-lifecycle` is an internal operator-facing composition for
the first Source, Policy, Playbook, BotBuild, BotRelease and signed-authority
lifecycle. It is not a new bounded context, service, worker or general registry.

## Ownership and boundaries

- BC01 owns stable Source identity, immutable SourceRevision content and
  eligibility history.
- BC11 owns immutable AcquisitionPolicy revisions and eligibility history.
  A policy must narrow the selected SourceRevision scope; the gateway remains
  the enforcement point.
- BC08 owns immutable payload ContractReference resolution.
- BC15 owns Playbook revisions, BotBuild verification and immutable
  BotRelease content/approval lifecycle.
- BC04 remains authoritative for signed snapshot acceptance, durable
  anti-rollback, admission, attempts, fencing, finalization and completion.
- BC05 and BC06 remain the owners of acquisition outputs/publication and
  artifact custody respectively.

The application coordinates these public capabilities and owner-named data
schemas in the existing PostgreSQL deployment. It does not create a new
service, transaction across BC04 and lifecycle records, or claim cross-store
ACID. A release is eligible at BC04 only after the signed snapshot has been
accepted there. A recorded revocation is pending until that acceptance occurs;
existing admitted operations and completed publications are not undone.
If BC04 accepted a snapshot but the local confirmation was lost, reconciliation
compares BC04's durable `(generation, snapshot digest)` and records the same
publication as installed without reinstalling an expired envelope. An expired
snapshot that BC04 never accepted is retained as superseded; the next
publication uses a strictly higher generation. Conflicting same-generation
digests or an unexplained higher BC04 head fail closed for operator
reconciliation.

## Lifecycle

The package exposes registration/resolution, candidate validation, explicit
operator approval, release eligibility/revocation and snapshot publication
through `acquisition_lifecycle.application.AcquisitionLifecycle`. `cli.py`
provides a narrow local operator command. The CLI's actor ID is an asserted
audit label except for release approval/revocation, where the actor must also
be present in `MOZAIC_RELEASE_APPROVERS`. This is a local operator boundary,
not enterprise authentication or RBAC. Controlled acceptance uses a
test-scoped approver and ephemeral authority key.

SourceRevision, policy, Playbook and BotBuild content is immutable by identity.
Identical registration is idempotent; different content under the same
identity conflicts. BotBuild content is independently verified through
`mozaic-bot-build`; runtime image compatibility is a separate immutable
`(build_id, runtime_image_digest)` binding. Each release candidate binds exact
source and policy digests, Playbook digest, verified build, runtime image and
BC08 contract reference. Compilation/registration does not approve a release.

BotBuild files are referenced by a verified local artifact path. The controlled
acceptance copies its generated build under ignored
`.local/acquisition-lifecycle/builds/<build_id>` and never overwrites an
existing artifact. Every candidate/eligible resolution re-verifies the exact
inventory. This is adequate for the single-host controlled workflow, not a
shared multi-host artifact distribution service; preserve and back up the path
registered by operators. Missing or changed build content fails closed.

## Database operations

Lifecycle migrations run with `BC04_OWNER_DATABASE_URL` (or
`LIFECYCLE_OWNER_DATABASE_URL`). The migration command grants the configured
`LIFECYCLE_APP_ROLE` (default `bc04_app`) only schema usage and table-specific
`SELECT`/`INSERT`/`UPDATE`; it grants no delete, truncate or DDL. Runtime
commands use `BC04_DATABASE_URL` or `LIFECYCLE_DATABASE_URL` and do not run
migrations. Each migration has a persisted SHA-256 checksum; never edit an
applied migration, add a forward migration instead.

```sh
make acquisition-lifecycle-migrate
uv run --locked --all-packages acquisition-lifecycle inspect
uv run --locked --all-packages acquisition-lifecycle resolve-release \
  --release-id <release-id>
```

`create-candidate` reads a closed, bounded JSON request containing the release
ID, SourceRevisionRef, PolicyRevisionRef, build ID, runtime image digest,
Playbook digest and exact payload ContractReference. `publish-authority`
requires owner-only key files whose public/private values match. Keep private
signing material out of worker, framework, Playbook and BotBuild environments.

## Validation

`make acquisition-lifecycle-tests` runs focused application tests.
`make acquisition-lifecycle-acceptance` runs the controlled live JSON vertical
through PostgreSQL, the protected Scrapy worker, the isolated gateway and
controlled TLS source, the M2 importer, BC06 custody, BC05 publication and
BC04 terminal completion. It then publishes a revocation snapshot and verifies
that BC04 authority rejects a new resolution of the revoked release. The
expected records remain in the test harness; they are not imported by this
application.

The M4 implementation does not provide an independently deployed Source,
Policy or Release service, a shared artifact store, general RBAC, Scout-to-
Playbook automation, Work Intake, source discovery or real-source approval.
