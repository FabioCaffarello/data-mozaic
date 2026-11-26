# ADR 028 — Immutable payload contract resolution

**Status: Proposed.** Approval remains a human governance decision.

## Context

BC05 already persists `PayloadContractRef` on output records and OutputSets, but
the M2 controlled compositions supplied schema paths directly. BotRelease v1
and BC04's durable execution binding did not retain the approved payload
contract, so the importer could not independently resolve the authorized
schema revision.

## Decision proposed

- BC08 owns payload contract identity and immutable revision semantics. This
  increment implements an in-process Python resolver package and a
  repository-installed local registry; it does not create a deployable or
  mutable network registry.
- A reference is the exact tuple `contract_id`, `version`, and
  `sha256:<hex>`. The digest hashes the exact UTF-8 schema file bytes, matching
  the existing M2 validator convention. It is an integrity check, not approval.
- The initial supported schema format is self-contained JSON Schema Draft
  2020-12. `$ref` and `$dynamicRef` resolution is unsupported and rejected,
  including local references, until immutable dependency binding is designed.
  External network retrieval is never performed.
- BotRelease v2 binds the payload contract. The signed operator authority
  snapshot carries that release, and BC04 copies the exact reference into the
  durable live execution binding. Fixture bindings also retain the reference.
  The trusted acquisition application resolves BC04's value and uses the
  resolved schema through BC05's validation port.
- Historical BotRelease v1 is not reinterpreted and cannot authorize new
  contract-bound admission. Old durable bindings with no reference are rejected
  for new import. Existing published BC05 data remains readable according to
  its persisted OutputSet contract references.
- Schema installation is a reviewed repository update. A duplicate
  identity/version is rejected in one installation manifest; changing bytes
  requires a new version. There is no in-place mutation or destructive delete.

## Consequences

Exact raw payload contract binding now traverses release, durable execution,
import, validation, and output provenance. Unresolved references, digest
mismatches, invalid documents, unsupported dialects/references, and invalid
payloads fail before publication. This is not an enterprise schema registry,
does not infer business compatibility, and does not provide approval,
eligibility, deprecation, backup, or a persisted administrative workflow.

The Python package is BC08-owned and currently serves the trusted acquisition
composition and its BC05 validation adapter. Its L1 maturity target is
independent resolver tests plus consumer integration; future consumers must
demonstrate contract compatibility before expanding the API.

## Alternatives considered

1. Keep schema paths injected by integration tooling: rejected because those
   paths do not prove the exact release-authorized contract.
2. Add a generic CRUD schema service or PostgreSQL registry: deferred; the
   current controlled lifecycle needs immutable lookup, and repository
   installation is already reviewed and restart-stable.
3. Resolve `$ref` at runtime from arbitrary URIs: rejected because it makes
   immutable validation behavior depend on mutable remote documents.
4. Add a new BotRelease v1 required field: rejected as a breaking reinterpretation;
   v2 makes the new binding explicit while retaining the v1 schema unchanged.

## Follow-up

M4 must integrate SourceRevision and release approval workflows with trusted
contract installation and revocation/eligibility policy. Any move to durable
registration or immutable schema dependency graphs requires operational
consumers, concurrency and backup requirements, and a compatibility plan.
