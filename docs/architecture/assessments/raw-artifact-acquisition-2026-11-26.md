# M6 raw artifact acquisition — implementation evidence

## Scope and ownership

This local M6 increment adds a strictly bounded raw-only Playbook v4 profile,
BotBuild manifest v4 and BotRelease v4. Its graph is one GET plus one direct
`output.artifact` sink. It rejects record extraction, query inputs, redirects,
pagination and continuation. Release v4 binds the exact artifact mode, media
type allowlist and byte maximum. BC13 retains its immutable Input Contract;
parameterless work uses the installed empty-object contract and the exact
unparameterized GET target. Historical profiles and payload bindings retain
their prior interpretation. ADR 031 remains Proposed.

BC13 owns Input/Submission/Dispatch. BC04 owns admission, attempt/fencing,
finalization and terminal result. BC15 owns the typed plan, deterministic
BotBuild and Scrapy execution. The gateway owns authorized transport and
FetchEvidence. The trusted acquisition application resolves that evidence,
coordinates BC06 custody and stages BC05 `ArtifactOutput`. BC06 owns logical
artifact identity and bytes; BC05 owns OutputSet and PublicationReceipt. The
worker has no persistence access or custody credential. No database migration
was needed: the output modality and its constraints extend the existing BC04
JSON binding with legacy defaults.

## Byte and resource semantics

The acquired artifact is the exact response entity after HTTP transport
decoding. The gateway does not retain TLS wire bytes, rejects unsupported
content encoding, hashes and spools this entity, then serves it from its
attempt-bound evidence endpoint. ZIP contents are not decompressed. Media type
is an allowlisted declaration, not format validation. The source filename is
not used as a path or object key. The worker returns only report and evidence
references; the raw profile does not retain the complete body in its result
bundle. The trusted importer reads the authoritative gateway evidence and
uploads those verified bytes through BC06's public API.

Bounded copies remain: the gateway buffers a response before writing spool;
the Scrapy gateway handler reads a bounded body into a response object; the
trusted importer reads a bounded evidence body; and BC06 upload uses a bounded
in-memory HTTP request body. This is not end-to-end streaming. In the current
profile, response, importer and custody upload bounds are 2,000,000 bytes; the
effective maximum is the smallest release, policy, BC04 aggregate, gateway,
worker, importer and BC06 bound. The controlled gate uses a narrower 100,000
byte response policy and 200,000 byte aggregate run budget. Its five fixture
entities are 19–139 bytes. No process RSS or throughput benchmark was taken.
Spool retention and orphan-custody cleanup remain bounded operational work for
M7; multipart and larger artifacts are out of scope.

## Controlled integrated acceptance

`make raw-artifact-acquisition-acceptance` runs controlled PDF, CSV, ZIP,
non-UTF-8 binary and raw JSON cases through PostgreSQL-backed Work Intake and
BC04, the approved Source/Release lifecycle, protected Scrapy worker, Go
gateway, BC06 PostgreSQL/RustFS and BC05 MongoDB. For each case it resolves the
current BC13 submission/run, verifies one FetchEvidence target, checks the
worker declares no records, imports only gateway-owned bytes, stages/seals one
ArtifactOutput, reserves/finalizes through BC04, checks the original BC05
receipt, independently reads BC05 and downloads the BC06 artifact to compare
body, representation, media type, size and SHA-256 against the separate
acceptance oracle. The worker's existing gateway smoke also rejects direct
source/persistence routes and an unauthorized gateway origin.

| Case | Controlled target | Media type | Size | SHA-256 | Result |
|---|---|---|---:|---|---|
| PDF | `/v1/artifacts/pdf` | `application/pdf` | 45 B | `aeb3142125f4707795376346666c7e94b9704fa1344e5bd30e9423e475c7c396` | passed |
| CSV | `/v1/artifacts/csv` | `text/csv` | 24 B | `68e6b7d7275c4e49f6ac7b87c0c97d049dfc0ae75c63ad629db6416b2760e480` | passed |
| ZIP | `/v1/artifacts/zip` | `application/zip` | 139 B | `6f623b341cc155d64e05bc5a6b7cf1f5db27e64063b3e5d073ccc123077297e2` | passed |
| Binary | `/v1/artifacts/binary` | `application/octet-stream` | 19 B | `d94abc6521988e6a67249655e76aeb221e709e221add57f4358a4fc750c91a85` | passed |
| JSON raw | `/v1/artifacts/json` | `application/json` | 44 B | `15a638a1dc62f113da4c27a189f7a0232543e80b1eb74e0473dae0a6f2e5cf4d` | passed |

For each run, the acceptance output records distinct Input, Submission,
CaptureRun, CaptureAttempt, Fetch ID, ArtifactRef, OutputSet and
PublicationReceipt. Each release, BotBuild and runtime is resolved through
the lifecycle for that case. Raw JSON is the original 44-byte body, including
its newline; no parser or reserialization is involved. BC06 artifacts are
independently retrievable after completion. The acceptance also replays import
for the same execution and checks stable OutputSet/manifest identity.

The raw JSON case submits the same logical Input under a new idempotency key to
demonstrate intentional reacquisition. The final local run passed with
submissions `1afa3a8e-93cf-5dbf-baf5-bc439f582d2a` and
`40936ac7-9da5-5be2-92b3-6bff5436cfbd`, runs
`cb4bfea8-6000-527b-a0e6-a30d3319bfd1` and
`a7811e9f-c1da-539d-a7f1-f8282ffa7243`, and OutputSets
`set-1d53a11d8b3218e3deb11cdce8007620d5961b608d35fe4938d18ae2cdcb04df`
and `set-0f2715746d17da7c20efadfb2bd2b16023f51ab3d8c14b8534b763cf8a296c44`.
Both retained digest `sha256:15a638a1dc62f113da4c27a189f7a0232543e80b1eb74e0473dae0a6f2e5cf4d`;
fetch IDs `8533e054206262b9395e9941ecd5ca06` and
`7ab61da9cee8383ee0c90e79e936283e`, ArtifactRefs
`artifact-2f9918835d349db40b48f8e478d7418cd329b32ea051754d9678e1cf6e43c21a`
and `artifact-ad69545dbc70950c2277321301986c28ca0803b113faafe76cf11f123354574f`,
and PublicationReceipts `pub-cc2fdc49bf5fadcbfb266c2caf13be74385bc284e50f0bdf7205476c1ffb691e`
and `pub-aa6ec14e6a279111d9fcdce209d1cae165104d53e8ff89027bf076033de5e659`
were distinct. The harness independently reread both published outputs after
reaquisition and confirmed the first remained readable and unchanged.

## Failure and recovery evidence

The actual BC04/gateway controlled live oversized response failed as
`response_too_large` on run `67af73ff-3173-427f-8745-571dfe96d1e1`; an
interrupted response failed as `response_interrupted` on run
`92dc7e15-f803-40a0-b4d5-b93f65aa1c2d`. Both were admitted executions with no
PublicationReceipt. These exercise shared transport/finalization boundaries;
they use the historical structured profile. Artifact-specific metadata tests
reject media type, representation, digest and size divergence before invoking
either BC06 custody or BC05 staging.

The existing custody interruption integration retained the custodied artifact
but produced no OutputSet or visible publication after a deterministic
injected exception. The partial-staging integration left one staged output but
no PublicationReceipt and zero visible outputs. These are injected boundary
failures, not forced process crashes. M5 admission acknowledgment-loss uses a
fresh composition and coordinator after deterministic exception injection;
the original BC04 run was recovered and no second run was created.

## Validation and remaining limits

The final five-profile raw artifact acceptance passed locally. CI includes the
controlled raw artifact gate after the M5 gate. The workflow is prepared
locally; remote CI has not run for this unpublished M6 branch. The full
regression results are reported with this delivery. No external source was
contacted, no parser was added, no new service or client SDK was extracted, and
no Nx or queue infrastructure was introduced.

M7 should measure gateway/worker/importer/BC06 process RSS, temporary disk,
peak concurrent buffers, interrupted transfer cleanup, evidence retention and
custody orphan reconciliation before deciding whether bounded streaming or
larger artifact support is justified.

## M6 final delivery gate — BC06 and raw-only negative acceptance

### BC06 integration root cause and correction

The original failure was the integration assertion `expected one durable pending
BC06 custody event` in `tooling/artifact_custody_integration.py`. Its query used
the authenticated pending-events endpoint with `limit=100`, then counted the
target artifact in the returned page. Read-only inspection found 289 pending
outbox events before reproducing the failure. The event created by that
operation existed for tenant `tenant-local-fixture`, but older pending events
occupied the first 100 rows. Therefore the test's global page did not prove a
missing durable effect.

The investigation also established a production tenant-isolation defect: the
endpoint authenticated `X-DM-Tenant-ID` but omitted that tenant when querying
the store. The endpoint could return another tenant's pending event. The
correction threads the authenticated tenant through the BC06 application port
and filters the PostgreSQL query by `tenant_id`. The recovery integration test
now verifies the operation's own durable event and verifies that a different
tenant sees none. The OpenAPI response description records tenant scoping.
There is no schema change or migration, and no historical record was modified
or deleted.

`make artifact-custody-integration` passed twice consecutively after this
correction against the same preserved PostgreSQL/RustFS/MongoDB state. Each run
created a distinct operation/artifact and receipt and confirmed idempotent
replay. `make artifact-custody-recovery-tests` passed
`TestRecoveryAfterObjectCommitBeforeInventoryConfirmation`. No database reset,
table truncation, volume recreation or credential regeneration was performed.

### Raw-only v4 failure and recovery acceptance

`make raw-artifact-negative-acceptance` runs five scenarios by invoking the
existing raw PDF v4 Playbook, verified BotBuild, approved BotRelease, BC13
Work Intake, BC04 current attempt, protected worker, gateway, trusted importer
and existing BC05/BC06 composition. The controlled-source response fault is
passed through the existing Compose service. Negative cases now fail the
harness if the worker unexpectedly succeeds.

| Scenario | Observed diagnostic and durable outcome |
|---|---|
| R1 oversized (100,001 bytes against the authorized 100,000-byte response policy) | Gateway returned `response_too_large`; BC04 and BC13 report `failed`; no receipt or import was reached. |
| R2 interrupted response (declared fixture length, connection closed after half the bytes) | Gateway returned `response_interrupted`; BC04 and BC13 report `failed`; no completed evidence or import was reached. This is a controlled HTTP interruption, not a process crash. |
| R3 disallowed media (`text/plain` against the release-bound `application/pdf` allowlist) | Protected Scrapy runtime rejected authoritative gateway metadata as `GATEWAY_EVIDENCE_INVALID`; BC04/BC13 failed before trusted import and BC06 custody. Content-Type is a declaration, not semantic file validation. |
| R4 non-identity encoding (`Content-Encoding: gzip`) | Gateway returned `content_encoding_denied`; BC04 and BC13 failed without import. The request explicitly asks for identity encoding; no transparent decompression changed representation. |
| R5 publication acknowledgment loss | BC05 committed, then deterministic injected `TimeoutError` hid the acknowledgment. A fresh BC04 coordinator reconciled the original reservation and exact BC05 receipt. The original OutputSet, manifest, ArtifactOutput and observation remained unique; independent BC06 retrieval verified the original 45-byte PDF SHA-256. This is deterministic fault injection, not process termination or component restart. |

For R1–R4 the harness verifies BC04's failed authoritative outcome, BC13's
correlated failed submission, and absence of a PublicationReceipt. Rejection
occurs before trusted import, so BC06 receives no custody request for these
attempts; no publication or receipt is created. R1/R2/R4 are rejected by the
gateway before completed FetchEvidence. R3 has completed gateway evidence,
which the protected runtime refuses against the immutable release allowlist.
The worker cannot broaden that allowlist.

The initial wiring check exposed that Compose did not pass the controlled raw
fault variable, and the harness did not fail if a negative acquisition
unexpectedly succeeded. The variable is now an explicit controlled-source
environment input and a negative case that completes raises an error. The
final full target was rerun after that correction and all five scenarios
passed. One earlier wiring attempt completed an unintended positive raw PDF
acceptance and left a valid test publication in the preserved database; that
durable test record was not removed.

The existing `make raw-artifact-acquisition-acceptance` positive gate remains
the independent PDF, CSV, ZIP, generic binary, raw JSON and intentional JSON
reacquisition oracle. The negative target is a separate CI step immediately
after that positive gate. The controlled profile remains 2,000,000 decimal
bytes; the acceptance policy remains 100,000 bytes per response and 200,000
bytes aggregate. No large-artifact performance claim is made.

### M6 final local validation state

The BC06 endpoint regression is covered by the recovery integration test and
the existing CI integration job. CI now executes both raw positive and raw
negative v4 targets. ADR 031 remains Proposed. The remote CI result and formal
PR review status will be recorded after publication; local validation is not
represented as remote approval.
