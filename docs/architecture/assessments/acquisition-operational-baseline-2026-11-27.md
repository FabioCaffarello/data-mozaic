# M9 Acquisition Operational Baseline — implementation evidence

Status: **ready for human review**. ADR 034 remains Proposed. The final local
M9 acceptance and required focused checks passed; this assessment does not
claim remote CI for unpublished code or production readiness.

## Initial repository state

- Principal checkout: `/Volumes/OWC Express 1M2/Develop/data-mozaic`.
- Integrated `main`: `fadae1d43a26ee6c19305e6f27b65cd0967a6f9d`.
- `origin/main` was fetched and matched the integrated SHA; `main` fast-forwarded
  without changes.
- Feature branch: `feat/acquisition-operational-baseline`.
- Exactly one Git worktree was registered; tracked working tree was clean before
  edits. No extra worktree, commit, push or PR was created.
- ADR 033 remains Proposed. Protected Scout A6 remains deferred; no Scout VM,
  external source or protected deployment was provisioned.

## Verified existing envelope

The controlled live Compose profile sets the Gateway spool to 67,108,864 bytes
(64 MiB), 500 evidence entries, and global fetch concurrency 8. The Gateway
implementation limits active evidence transfers to 4. The policy limit is
source/release-specific; the raw-only v4 capability remains 2,000,000 bytes.
BC05 published reads are paged with a maximum page size of 1,000. BC06 API
transfer concurrency and configured byte limit are deployment settings; its
current object adapter uses bounded single PutObject with no multipart.

## Evidence lifecycle and ownership

| Resource | Owner | Current lifecycle and recovery |
|---|---|---|
| Incomplete `.part` transfer | Gateway | Removed during safe startup scan; reservations are process-local, and upstream quota admission is durably charged conservatively. |
| Completed Gateway body | Gateway temporary spool | Previously retained indefinitely; M9 adds exact body release after successful import, BC04 reconciled completion and BC05 publication evidence. |
| FetchEvidence metadata | Gateway | Retained after body release; includes tenant/run/attempt/generation, source/revision, policy/build, request target digests, status, acquisition time, media type, representation, size and digest. |
| Verified entity artifact | BC06 | Durable logical ArtifactID, byte digest/size, custody receipt and artifact metadata; exact historical bytes are read via BC06. |
| Staged/sealed OutputSet | BC05 | Private and not visible through published reads; no evidence release eligibility. |
| Publication reservation | BC04 | Unresolved reservation blocks eligibility; existing reconciliation owns completion. |
| Published OutputSet/receipt | BC05, reconciled by BC04 | Published membership and receipt remain authoritative; published reads are bounded and tenant scoped. |
| Terminal run | BC04 | Not independently sufficient for body release. Failed/cancelled state may have residual effects. |

M9 release is body-only. It validates exact FetchEvidence metadata and digest,
rejects active readers, uses a separate release credential, retains metadata,
serializes against reader admission, fsyncs the spool directory and preserves
metadata-only entries across startup. Metadata cannot satisfy historical body
retrieval; consumers use the associated BC06 ArtifactID once publication is
complete. Unresolved, pending, failed, cancelled, partial and unpublished
operations retain Gateway bodies.

## Current O1–O22 matrix

| ID | State | Evidence / remaining restriction |
|---|---|---|
| O1 dependencies identified | Passed by inspection | See lifecycle table and owner contracts; verify with the final M9 report. |
| O2 safe body release | Reusable application and raw-only operator command implemented | `TrustedAcquisitionApplication` verifies BC04/BC05/BC06 facts and rechecks BC04 before release. An injected operator allowlist and separate Gateway release client are required. The controlled harness invokes `tooling/acquisition_release_evidence.py` for raw-only runs. Structured mappings remain released through the trusted importer while its verified page bundle is present. |
| O3 pending/unresolved protected | Passed in controlled interruption cases | Custody interruption and partial-stage interruption each retain 147 Gateway body bytes without published output. Publication acknowledgment loss is reconciled before the successful release path. |
| O4 reclaimed capacity | Passed for active bytes and entry slots; historical retention explicitly limited | `make acquisition-operational-acceptance` completed seven real raw-only v4 publish/release cycles with three active entries configured. Resource snapshots verify body bytes and active entry slots were reclaimed. Historical metadata remains in a separately counted finite archive (default 16 MiB); purge/compaction and unbounded sustainability are not claimed. Archive exhaustion conformance is recorded separately below. |
| O5 next acquisition | Passed in repeated controlled acquisition | Each of seven cycles admitted another controlled acquisition after release, using the same isolated persistent Gateway volume and restarting the Gateway between cycles. |
| O6 published data remains readable | Passed | M9 acceptance re-reads published OutputSet and authenticated BC06 ArtifactID bytes after Gateway body release. |
| O7 provenance retained | Passed | Gateway release test and startup accounting retain exact metadata; controlled acceptance verifies retained entries and successful completed-result replay. |
| O8 idempotency/partial failure | Passed within the tested recovery model; not exhaustive crash coverage | Duplicate release, active readers, wrong identity/credentials, injected archive-directory synchronization failure, archive quota exhaustion, and startup reconciliation are covered. A restart with the same persistent volume preserves archived history and evidence retained after failed release. No actual process termination at each fsync/unlink instruction boundary is claimed. |
| O9 operational acquisition inspection | Passed with explicit unresolved fields | `make acquisition-inspect ACQUISITION_INSPECT_ARGS="--tenant-id … --submission-id …"` was run on a completed real controlled Submission; BC04/BC05 receipts matched and BC06 metadata was available. Gateway IDs are not cross-indexed. |
| O10 recovery interfaces | Passed for exercised paths | M9 target covers lost publication acknowledgment/replay; `make execution-coordination-finalization-tests` passed 13 tests; `make work-intake-postgres-tests` passed 10; custody interruption retains evidence. |
| O11 residual custody inspection | Implemented and adapter-tested | BC06 adds authenticated tenant-scoped keyset inventory with a bounded page (maximum 100), operation and logical artifact identity, state and timestamps. Disposition without a published reference remains unknown; no deletion operation is provided. |
| O12 developer-authored acquisition | Passed by controlled integrated operator journey | `make acquisition-operator-acceptance` invoked the installed, configuration-backed `acquisition-operator` in fresh subprocesses for submit, idempotent submit replay, dispatch, protected execution, trusted evidence import, BC05/BC06 publication, inspect, reconcile, completed-result replay, pending cancellation, and intentional reacquisition. Two distinct parameterized Inputs used the same immutable approved release/build/runtime and produced different canonical parameter digests, distinct submissions/runs, expected request targets and independently verified published records/artifacts. The harness provisioned lifecycle/source/release authority and acted only as a command consumer and independent oracle; it did not compose Work Intake, launch the worker, or import results for this journey. |
| O13 independent records | Passed | M9 target reads published BC05 output without invoking the worker after publication. |
| O14 independent artifact retrieval | Passed | M9 target reads exact BC06 ArtifactID bytes and verifies digest/size after Gateway body release. |
| O15 publication visibility/tenant | Passed in existing tests and controlled paths | `make acquisition-output-mongo-tests` passed 17 cases, including sealed-set invisibility; BC06 metadata/retrieval remains tenant scoped. |
| O16 bounded consumption | Passed for current contracts | BC05 pages remain capped at 1,000 (inspection uses 100); BC06 transfer uses bounded single-object operations; no multipart added. |
| O17 startup/readiness | Passed for controlled declarative acquisition | `make authorized-live-readiness-acceptance` completed on the final tree. It demonstrated valid owner-backed readiness plus Gateway unavailable, missing operator identity, exact contract unavailable, unknown/revoked release, and actual Gateway entry-capacity exhaustion. It does not reserve capacity; runtime revalidates. Scout readiness stays separate. |
| O18 capability envelope | Passed by repository configuration/contract review | Current limits and supported profile distinctions are recorded in the operations guide; no capacity claim exceeds observed profile bounds. |
| O19 M8 deterministic regression | Passed | `make acquisition-operational-acceptance` completed its Agent-to-Raw regression: Scout HTTP 2 passed/0 skipped and controlled PDF, structured JSON, parameterized Work Intake journeys completed. |
| O20 CI acceptance | Prepared; remote result unavailable | CI invokes the finite M9 target, which completed locally with exit 0 including Agent-to-Raw. `actionlint` passed locally. No remote CI claim is made for this unpublished branch. |
| O21 onboarding checklist | Passed as documented readiness aid | The reusable checklist is in the operations guide; it does not authorize or contact a source. |
| O22 single checkout | Passed | Final `git worktree list --porcelain` reports exactly the principal checkout. Publication and integration do not use an additional worktree. |

## Commands and result ledger

Commands and results:

- `cd apps/acquisition-gateway && GOTOOLCHAIN=go1.27.4 go test ./...` — passed
  after correcting startup accounting to preserve valid metadata-only entries.
- `make acquisition-operational-acceptance` — passed in the final tree; raw
  positive/negative, structured HTML, natural and planned pagination, recovery
  subtargets, seven three-entry-capacity reuse cycles, and Agent-to-Raw passed.
  The final-tree run measured raw-PDF 45 bytes, CSV 24, ZIP 139 and raw JSON
  44 bytes reclaimed; the isolated 100,000-byte JSON Gateway spool returned to
  zero body bytes, kept metadata, and completed-result replay stayed valid.
- `make check` (final tree) — passed: 261 tests ran, 235 passed and 26 were
  skipped by the fast non-integration command; skipped tests are not counted as
  passes. `make test-integration` passed 21 PostgreSQL Scout tests;
  `make work-intake-postgres-tests` passed 10;
  `make execution-coordination-finalization-tests` passed 13 plus its BC05
  finalization integration; `make acquisition-output-mongo-tests` passed 17;
  `make artifact-custody-integration` and
  `make artifact-custody-recovery-tests` passed.
- `make acquisition-gateway-go-race`, `make acquisition-gateway-go-build`,
  `make acquisition-gateway-go-vuln` passed; vulnerability scan found none.
- `make artifact-custody-go-race`, `make artifact-custody-go-build` and
  `make artifact-custody-go-vuln` passed; vulnerability scan found none.
- `make acquisition-operator-acceptance` passed in the integrated target with
  actual operator subprocesses. `make authorized-live-readiness-acceptance`
  passed valid and negative owner-backed readiness scenarios.
- `actionlint`, Ruff, format, py_compile, docs, architecture, naming and both
  diff whitespace checks passed. Custody-interruption first exposed a missing
  starting-snapshot measurement for structured profiles; after fixing that
  harness measurement, both custody- and partial-stage interruptions retained
  147 bytes and passed.

No test skip is counted as pass. Persistent local services and volumes remain
untouched by infrastructure reset operations.

## Limitations and next steps

## Client and library decision

No shared client library was extracted. The M9 consumers use two cohesive owner
boundaries with different identities, authorization and retry semantics:
BC05 `AcquisitionOutputApplication` returns published, paginated output refs;
BC06 `ArtifactCustodyHTTPClient` performs authenticated tenant-scoped metadata
and bounded byte retrieval. Trusted import additionally verifies Gateway
evidence and BC04 execution identity. Scout's MCP transport has separate
source-approval and no-go rules. A shared package would either duplicate these
owner policies or become a generic wrapper without multiple compatible
operational consumers. Keep the existing clients owner-specific; revisit only
after another independent application consumes the same public contract.

- The narrow operator CLI supports raw-only v4 because Gateway historical
  metadata intentionally does not retain a full source URL for structured
  ArtifactRef derivation. Structured release remains supported in the importer
  using the verified page bundle. The controlled harness supplies a test-scoped
  operator allowlist; a production composition must provide its own allowlist
  and retain the separate Gateway release credential.
- Released metadata moves from the 500 active-entry budget into a separate
  bounded 16 MiB archive. The archive remains finite and persistent; archive
  exhaustion stops release/admission safely. There is no archive purge policy
  or unlimited historical storage claim.
- Completed body replay is fulfilled by BC06 ArtifactID reads, not by Gateway
  historical FetchEvidence body reads after release.
- A6 protected authoring remains deferred and is not a M9 dependency.
- O12 now has a configuration-backed command entrypoint and end-to-end
  controlled acceptance. The CLI requires the existing owner credentials,
  exact BC04 signed authority and a Compose image whose image ID equals the
  approved runtime digest. The operator identity value is a dispatcher label,
  not an authorization grant. Residual tenant
  inventory is bounded and non-destructive; unknown cross-context dispositions
  remain unknown. The historical metadata archive is finite and has no purge or
  garbage-collection policy. A6 remains deferred.
