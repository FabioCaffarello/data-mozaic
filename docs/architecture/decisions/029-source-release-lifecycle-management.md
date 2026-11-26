# ADR 029 — Operational Source and Release lifecycle

**Status: Proposed.** Approval remains a human governance decision.

## Context

The controlled live workflow previously assembled SourceRevision, policy,
BotRelease and signed authority records inside test tooling. BC01, BC11 and
BC15 have distinct ownership, while BC04 remains the trusted authority and
execution boundary. The acquisition platform needs a reusable lifecycle
without creating CRUD services or transferring provider state to a central
registry.

## Decision proposed

- Add `apps/acquisition-lifecycle` as an in-process operator composition,
  not a bounded context or deployable.
- Preserve BC01 ownership of Source/SourceRevision, BC11 ownership of
  AcquisitionPolicy, BC08 ownership of immutable payload contracts, BC15
  ownership of Playbook/Build/Release engineering, and BC04 ownership of
  signed authority installation, anti-rollback and execution admission.
- Persist lifecycle records in owner-named PostgreSQL schemas. Use immutable
  revision identity, idempotent same-content registration, conflict on changed
  content, separate eligibility history, and retained historical lookup.
- Verify BotBuild bytes with `mozaic-bot-build`; store build content separately
  from one or more exact worker-runtime image digest bindings. Refuse resolution
  when the registered local artifact is unavailable or differs.
- Keep candidate creation, approval, eligibility and authority publication as
  distinct states. Only a locally allowlisted operator can approve/revoke
  releases. The current local CLI trust model is not enterprise authentication
  or RBAC.
- Publish authority as a signed immutable snapshot and install it through the
  existing BC04 verifier/registry. A revocation takes effect for new admission
  only after BC04 accepts the newer snapshot. Failed publication remains
  pending and must be reconciled before generating another generation.
- Reconciliation compares the exact durable BC04 generation and snapshot
  digest. If BC04 accepted a snapshot but local confirmation was lost, the
  lifecycle records that same publication as installed without re-installing
  an expired envelope. An expired snapshot that BC04 did not accept is
  retained as superseded; the next publication uses a generation greater than
  both the current BC04 head and every local publication attempt. Conflicting
  or unexpectedly advanced heads fail closed for operator reconciliation.
- Use table-specific runtime grants and owner credentials for migrations.
  No runtime application role receives DDL or destructive grants.

## Alternatives rejected

- A new source/release microservice: no independent deployment or operational
  requirement has been demonstrated.
- Letting a harness fabricate release documents: it bypasses reusable
  registration, approval and publication behavior.
- Treating compile/build success as approval: integrity and authorization are
  separate decisions.
- Storing every build in BC06 acquisition custody: it would conflate software
  supply-chain artifact retention with acquired source artifact custody without
  a demonstrated public contract.
- Claiming a local path is a distributed artifact catalog: the implementation
  verifies a preserved local path and fails closed on absence; remote worker
  distribution/replication remains an operational capability gap.
- Claiming revocation undoes admitted requests or committed output: the existing
  BC04 operation and BC04/BC05 finalization semantics remain authoritative.

## Consequences and limits

The controlled single-host workflow can register immutable lifecycle records,
approve a release, sign/install authority, execute a protected acquisition and
revoke future eligibility. PostgreSQL migrations are checksummed. Authority
publication is not an ACID transaction with lifecycle records; a publication
record is pending until BC04 installation succeeds. Updates racing a pending
generation require installing/reconciling that exact snapshot before another
generation can be prepared.

Approver identities are local allowlist values; production identity, separation
of duties, signing-key provisioning/rotation and durable shared build artifact
availability require a later operational decision. Source/policy record audit
actor IDs on registration are asserted CLI values. No Scout automation or
Work Intake is included. M5 may consume exact SourceRevision, contract and
approved release references but must introduce its own logical Input identity,
idempotent submission and dispatch lifecycle.
