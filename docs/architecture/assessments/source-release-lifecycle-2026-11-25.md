# M3 delivery and M4 Source/Release lifecycle — implementation evidence

**Assessment date:** 2026-11-25/26 local session

**M3:** integrated by PR #25.

**M4:** local implementation on `feat/source-release-lifecycle`; not committed or published.
**ADR 029:** Proposed; no human approval is implied.

## Verified repository and M3 delivery

The principal checkout was on M3 branch `feat/immutable-contract-resolution`
from integrated M2 main `9f5b5cc2ba12941c1f174f8cc1f05ee707d87dca`. The local
checkout had one registered worktree. Review found the BC08 resolver rejected
`$ref` but did not reject Draft 2020-12 `$dynamicRef`; the M3 change added the
fail-closed check, test and documentation before delivery. BotRelease v1
documents remained immutable; new live admission uses v2 and exact payload
contract identity/version/digest. Catalog installation remains repository
versioned, not an operational approval service.

M3 PR [#25](https://github.com/FabioCaffarello/data-mozaic/pull/25) was reviewed
at head `3442371d47f9704c49450c5616a1ccb4fa9b87a9`. Seven required remote
checks passed, including Quality Gate, Integration Security Gate, BC05 MongoDB,
BC06 Go, Gateway Go, gateway container network boundary and BC06/PostgreSQL/
RustFS + BC05 interoperability. The PR was squash merged; exact integrated
`origin/main` SHA is
`8c16167d92a1a40eefcfe711151881507e9e5911`. The principal checkout was
fast-forward synchronized to that SHA and only then switched to the M4 branch.

## M4 ownership and persistence

`apps/acquisition-lifecycle` is an in-process operator composition, not a new
bounded context or service. BC01 SourceRevision, BC11 AcquisitionPolicy, BC15
Playbook/BotBuild/BotRelease and authority publication records live in
owner-named PostgreSQL schemas; BC08 retains the exact local immutable payload
contract registry. BC04 accepts signed snapshots and remains the admission,
attempt, fencing and terminal authority. BC05 and BC06 ownership is unchanged.

The runtime database role receives schema `USAGE` and table-specific
`SELECT`/`INSERT`/`UPDATE`; migration DDL uses the owner DSN. There is no DELETE,
TRUNCATE, runtime DDL or worker database credential. Migrations are checksummed
and forward-only. Build content and runtime-image identity are separate:
BotBuild ID/manifest/file inventory are verified with `mozaic-bot-build`,
while immutable `(build_id, runtime_image_digest)` rows bind exact worker image
variants. A BotBuild content conflict under one ID is rejected; a new exact
runtime digest can be recorded without changing the build.

The first implementation is single-host. It stores a local path for each
verified BotBuild and re-verifies files on candidate and eligible-release
resolution. The controlled harness retains build files under ignored
`.local/acquisition-lifecycle/builds/<build_id>` and does not overwrite an
existing path. This is not a shared multi-host artifact distribution service;
the path is an operational dependency and must be preserved/backed up. Missing
or divergent bytes fail closed. Runtime image distribution is likewise not
provided by a new registry service.

## Lifecycle behavior implemented

- Stable Source identity enforces one owner; SourceRevision registration is
  immutable and idempotent for identical RFC 8785 canonical content. Eligibility
  changes are separate, audited rows; historical revision documents remain.
- Policy revisions are immutable, idempotent, and bound to an eligible source
  revision. Registration rejects origins, methods, path prefixes or query
  values that exceed source scope. Policy and source changes remain separate
  from gateway enforcement.
- Playbooks are parsed and validated by BC15, tied to a registered eligible
  SourceRevision, hashed from exact file bytes, and stored immutably.
- BotBuild registration verifies the manifest and exact closed file inventory,
  live profile and runtime compatibility. Release resolution re-verifies the
  stored path, exact manifest digest, runtime binding and BC08 payload contract.
- A pending release candidate verifies source, policy, playbook, build, runtime,
  contract and BC15 live-admission conformance. Explicit approval requires the
  configured local operator allowlist; compile/registration alone cannot
  approve. Approved release content is immutable. Revocation updates lifecycle
  eligibility and emits an event without rewriting historical approval bytes.
- Authority snapshots use the existing Ed25519 signature and BC04 durable
  monotonic registry. Publication is persisted as `pending` before BC04 install
  and marked `installed` only after BC04 accepts it. Same-generation replay is
  idempotent; a conflicting/rollback generation fails. A pending generation
  must be reconciled before preparing another. Cross-database atomicity is not
  claimed. Revocation is effective for new admissions only after BC04 accepts
  the new snapshot; admitted HTTP operations and committed outputs are not
  undone.

The authority documents use the existing RFC 8785 digest convention. BC15's
current `validate_live_admission` helper expects the historical `canonical_json`
digest convention. The lifecycle projects that helper's digest fields onto
temporary copies for its conformance check; stored and signed owner documents
retain RFC 8785 digests. This compatibility seam is documented and remains a
candidate for a later contracts-compatible BC15 cleanup.

## Controlled lifecycle acceptance

`make acquisition-lifecycle-acceptance` ran against existing PostgreSQL,
MongoDB, RustFS/BC06, the protected Scrapy live worker, the Go acquisition
gateway and the local TLS controlled source. It registered a controlled
SourceRevision, policy, Playbook, verified build/runtime binding, candidate
and explicit test-scoped approval; signed and installed the authority; admitted
a real BC04 run; imported gateway evidence through the M2 application; staged
and published BC05 outputs; custodied bytes through BC06; and independently
checked published records, artifact bytes, receipt and BC04 terminal result.
The test-only expected records remained in the harness.

Final successful acquisition identities:

| Identity | Value |
|---|---|
| SourceRevision | `controlled-http@controlled-v1` |
| AcquisitionPolicy | `controlled-live-json-get@1.0.0` |
| BotBuild | `042ade691a296d486a8386241e90a745f303e399bf3aed5b847f398a00901b99` |
| Build manifest digest | `sha256:93e9335920e0339871bc17a59592d9cb3fb52371757b597c0fcad302ca409061` |
| Worker image digest | `sha256:bb21078fd1434218983b5c6559a7109a58b027be1bc1ca823a9b9425a5d4e53f` |
| Payload contract | `controlled-live-json.record@1.0.0#sha256:dd8976c347bf0daac2f84d42429fdd8059bf11e810dbe0468162c6538bb4e6c` |
| BotRelease | `controlled-live-release-a95fcbdd1e9d` |
| CaptureRun / Attempt | `9b421105-f253-4a95-99ee-3e89261ddca2` / `34b9c51e-dfcd-56b9-a7f4-abeb91563a0b` |
| OutputSet / manifest | `set-3ee63060a94b0f28f44950eaa0d583f4b60e70d97bc54e060f9df4843d20130b` / `sha256:f6f96e4c3b8daeda70547fc8cd45d2536fb279dae885d1c9786ed3e3e51dba91` |
| PublicationReceipt | `pub-ad9e0d32b4216b41f1fa303e7f539340dea61a6b3ab8173b89488b95b301f3e0` |
| Records / artifact | `2` / `artifact-eca083441011b2a68de70a48a53b90dad5d3aadd345a26e49843e7bea92ed93c` |
| Artifact digest | `sha256:59ed84aab12fcfd6f2fa6bed45d84a2ade7cc851c32669094bb41539a4447b2c` |
| Durable terminal state | `succeeded` |

After successful publication, the acceptance explicitly revoked that release,
published the next signed authority generation (`149`), verified that its
historical approved release document was unchanged, and verified that BC04
authority resolution rejected a new admission using that release. This was a
controlled admission-resolution check, not a second external request. No real
external source was accessed.

## Local validation evidence

- `make acquisition-lifecycle-migrate`: passed with the BC04 owner credentials;
  the earlier deliberate attempt to run DDL with the app role failed before
  applying DDL and drove the owner/app separation.
- `make acquisition-lifecycle-acceptance`: exit 0 on the final run; complete
  live JSON → gateway → importer → BC06 custody → BC05 publication → BC04
  terminal path and release revocation/admission denial passed.
- Focused lifecycle unit suite: 5 tests passed for immutable source
  registration, unresolved source rejection, policy subset scope, query
  constraints and narrowing behavior.
- Architecture dependency check, migration naming check and focused Ruff checks
  passed during implementation. Final `make check`, documentation/naming gates
  and `git diff --check` are recorded in the delivery report after the final
  documentation update.

The final M4 delivery review added BC04 durable-head reconciliation for a lost
local publication acknowledgment and safe superseding of an expired snapshot
that BC04 never accepted. Focused tests cover acknowledgment loss, exact digest
conflict, expired pending recovery and timezone validation. The
`artifact-custody-integration` CI job now runs
`make acquisition-lifecycle-acceptance` against its disposable PostgreSQL,
MongoDB, RustFS, gateway and controlled-source infrastructure.

M4 remote CI will run on the unpublished PR head. M4 did not implement
real sources, Scout exploration, Work Intake, a universal schema registry,
shared build distribution, raw-only binary acquisition or a new service.
