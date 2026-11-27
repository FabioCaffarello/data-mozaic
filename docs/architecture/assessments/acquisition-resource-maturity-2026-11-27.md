# M7 acquisition resource maturity — implementation evidence

## Scope and decision

This M7 increment keeps the 2,000,000 decimal-byte raw-only v4 capability
unchanged. It removes unnecessary body-sized memory copies at the raw worker,
gateway and trusted-import boundaries, reserves gateway spool capacity before
fetch, verifies evidence and custody independently, and adds bounded benchmark
and resource inspection. No external source, parser, service, storage system,
queue, multipart protocol or shared transfer SDK was added. ADR 032 is
Proposed; ADR 031 remains Proposed.

**Explicit scope decision:** the roadmap's 8, 32 and 64 MiB experiments are
deferred. Immutable Playbook/Build/Release v4 authorization remains capped at
2,000,000 bytes, and this increment does not create a new capability profile
to satisfy a benchmark target. T15 is therefore **Deferred by explicit scope
decision**, not passed. The 100 MiB BC06 provider ceiling is not evidence of
end-to-end acquisition support. M7's resource-maturity envelope is the
existing authorized profile only.

The selected architecture is **source → bounded gateway disk spool → verified
gateway evidence → bounded importer temporary file → existing BC06 verified
spool/object upload → BC05 publication**. The gateway remains the authoritative
source-acquisition evidence provider. Raw-only workers receive a completion
response carrying evidence metadata/reference and an empty HTTP body. Structured
profiles still receive their response body because extraction consumes it.

Direct source-to-custody transfer was rejected because it removes recoverable
gateway evidence and moves source acquisition responsibilities into the
importer. Multipart was unnecessary for the unchanged v4 cap and current BC06
single-part envelope. A universal byte-stream abstraction and BC06 client SDK
were not extracted: gateway, evidence, custody and output ownership have
different lifecycles, and the trusted acquisition application is the only
compatible raw upload consumer.

## Actual byte-flow inventory

| Path | Producer → consumer / representation | Before M7 | M7 behavior and bounds | Integrity, ownership, failure and recovery |
|---|---|---|---|---|
| Structured live | Gateway decoded HTTP entity → Scrapy `Response` → selectors | Gateway `io.ReadAll` body, spool copy, handler body read, evidence retrieval `bytes`, importer bytes | Gateway writes incrementally to evidence spool; handler accumulates a bounded `bytearray` (64 KiB chunks, <=2,000,000 bytes) then converts it to immutable `bytes` for Scrapy, so those two bounded representations can briefly overlap; structured evidence/import APIs also materialize bounded bytes for parsing | SHA-256/size incrementally in gateway and spool; handler checks declared size; importer verifies authoritative evidence. Incomplete response is not committed. Structured full body remains necessary for extraction. |
| Composite live | Gateway entity → Scrapy response/parser → records; importer verifies evidence | Same bounded full-body copies as structured | Same bounded body behavior as structured; raw optimization is not applied | Record extraction/provenance semantics unchanged. Each completed FetchEvidence remains gateway-owned and attempt-bound. |
| Raw-only live | Source → gateway → worker → importer → BC06 | Gateway body in memory and spool; full body returned to worker although only metadata was persisted; evidence client materialized bytes; BC06 client accepted bytes | Gateway uses a private `.part` file and 64 KiB read/hash buffer; commits body+metadata only after complete verification. Raw worker gets a zero-byte reference response plus trusted metadata. Importer streams evidence to a private temporary file with incremental digest and size checks; client uploads that file with Content-Length. | The exact opened evidence inode is hashed before transfer and rewound. BC06 independently streams to its private verified spool and checks actual size/digest before object persistence. Failures remove incomplete gateway/importer resources; completed evidence is retained for recovery. |
| Gateway evidence retrieval | Completed evidence file → trusted importer | `readEvidence` copied full file into `[]byte` | Opened safe inode, incremental SHA-256 verification with 64 KiB buffer, same descriptor rewound then streamed with exact Content-Length | Fetch/tenant/run/attempt/generation bound; no path supplied by caller. Corrupt size/digest or identity fails closed. |
| BC06 upload | Trusted importer → public BC06 HTTP → BC06 S3 adapter → RustFS | Python request required `bytes`; server and S3 adapter already streamed into a 64 KiB bounded temporary file | Python sends `Path` bytes in 64 KiB chunks with fixed Content-Length; Go HTTP handler bounds the body; service verifies intent; S3 adapter copies/hash-verifies to owner-local private spool and supplies the file to one S3 `PutObject` | BC06 does not trust caller digest alone. Existing intent, lease, generation fencing, conditional object write, inventory, outbox and receipt remain authoritative. No multipart. |

No simultaneous-memory estimate is derived by adding source buffers. Docker
memory samples include process/runtime/container overhead and may include
cache. The host Python sample measures the whole acceptance process, not an
isolated importer process. Kernel and Docker Desktop overhead are not separated.

## Resource hierarchy and ownership

All byte quantities below are decimal bytes unless marked MiB/MiB-style
container settings:

| Boundary | Bound | Effect |
|---|---:|---|
| Playbook v4 / BotBuild v4 / BotRelease v4 artifact capability | 2,000,000 bytes | Immutable approved maximum; unchanged |
| AcquisitionPolicy v1 response | 2,000,000 bytes maximum | Per-response authorization; lower configured value wins |
| Controlled acceptance policy | workload size, 65,536 / 1,000,000 / 2,000,000 | Aggregate run budget equals `max(200,000, workload size)` |
| Scrapy live runtime response | 2,000,000 bytes | Component ceiling; structured mode only materializes body |
| Trusted result bundle | 16,777,216 bytes overall | Worker metadata/result bundle ceiling; artifact body is not in it |
| Gateway local evidence spool | 67,108,864 bytes; 500 entries | Aggregate single-instance accounting, includes paired metadata/body entry slots |
| Gateway active fetches | 8 | Bounded component-wide admission; policy concurrency and execution budgets can be lower |
| Gateway active evidence reads | 4 | Nonblocking admission; excess authenticated transfers receive `429 evidence_concurrency_exhausted` |
| BC06 local provider upload | 104,857,600 bytes per upload | Provider ceiling; does not authorize acquisition |
| BC06 concurrent uploads | 4 | Non-blocking bounded upload slots |
| BC06 local temporary budget | 440,401,920 bytes | Startup requires capacity for four maximum upload spools |
| BC06 temporary spool buffer | 65,536 bytes | Go bounded copy/hash buffer |
| Worker Compose memory | 64 MiB | Container memory limit |
| Gateway Compose memory | 256 MiB | Container memory limit |

The effective v4 artifact maximum is the minimum of approved release/build,
source policy, aggregate run budget, gateway, worker and importer bounds:
2,000,000 bytes. A future larger maximum requires explicit versioned approved
capability contracts; increasing component ceilings cannot broaden source
authorization. The current BC06 local provider accepts larger files, but M7
does not interpret that as v4 permission.

Gateway spool reservations are atomic under the process mutex and are tied to
unique reservation identities. A duplicate release cannot consume another
transfer's reservation. The reservation uses the authorized per-response
maximum until completion, then commits actual written bytes. It rejects before
the source request when aggregate bytes/entry capacity are unavailable. This
is single-process accounting; no distributed quota guarantee is claimed.
Gateway source admission retains its existing global and per-policy
concurrency bounds. Evidence retrieval has a separate four-transfer
nonblocking semaphore and exposes only its active count in the aggregate
resource snapshot. BC06 uses a finite nonblocking upload semaphore; no
unbounded queue or DB transaction across byte transfer was added.

## Temporary and durable resource lifecycle

- Gateway `.fetch-*.part` and temporary metadata files are removed during
  startup recovery after secure regular-file checks. A body/metadata half-pair
  without the other side is removed because it was never authoritative evidence.
- A completed evidence pair is durable and retained. The platform currently
  lacks cross-context proof that no import, custody recovery, staging operation
  or unresolved publication reservation needs it. No arbitrary TTL is safe.
- Importer evidence files live in a private temporary directory. The
  `import_live_artifact_result` wrapper removes it in `finally`; callers that
  split verify/import keep it through any idempotent replay and call the
  bundle's explicit `cleanup()` after the last byte consumer. They are never
  worker readable and are not a recovery copy.
- BC06 removes its private incomplete upload spool through existing Go
  deferred cleanup. A committed custody artifact can validly remain unpublished
  after later BC05 failure; no cross-store collector deletes it.
- BC05 private staged outputs remain invisible until its existing publication
  protocol commits. BC04 reservation/fencing/finalization is unchanged.

`GET /internal/v1/resources` is authenticated with the existing internal
evidence-import credential and returns component-wide counters only: spool
bytes used/reserved, entries, configured limits, active source fetches and
active evidence transfers. It returns no tenant, run, fetch, artifact or URL
values. This is an operational snapshot,
not a metrics backend or tenant-specific inventory API. BC06's existing public
exact artifact metadata, verification and upload-recovery interfaces remain
tenant-scoped and owner-local. They permit inspection when the artifact or
operation identity is already known, but do not provide a complete paginated
residual-custody inventory or cross-context publication disposition. M7 does
not infer absence from a bounded listing and adds no unsafe cross-store
garbage-collection command. Comprehensive non-destructive residual inventory
is deferred; no artifact is automatically deleted.

## Baseline and post-change measurement

The reproducible command is `make acquisition-resource-benchmark`. It runs the
complete controlled raw PDF acquisition lifecycle (BC13, BC04, immutable
release/build, protected Scrapy worker, gateway, importer, BC06/RustFS, BC05,
terminal finalization and independent public artifact read). Content is
deterministic: byte `i` is `(i*31+17) mod 256`, independently sized/hashed in
fixed chunks. Each run checks the expected size/digest and exact receipt.

Environment: macOS 15.6 arm64; Python 3.12.12; Docker Engine 28.5.1. The
gateway/controlled source/worker ran in Linux containers with configured
256 MiB/64 MiB/64 MiB limits. Gateway evidence volume was already populated by
prior preserved acceptance runs; no evidence or database state was deleted.
Sampling interval was 0.5 seconds. Container values are sampled Docker stats
memory usage, not exact RSS/peaks. Host `ps` sampled the entire Python
acceptance process, including lifecycle orchestration and BC05/BC06 clients;
it is not an isolated importer measurement. Sampling can miss brief peaks and
the single 64 KiB comparison has no statistical significance.

| Stage | Bytes | Repetitions | Total acquisition seconds | Host acceptance-process sampled RSS | Gateway / worker sampled memory | BC06 / RustFS sampled memory | Gateway spool change | Digest |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Before M7, 64 KiB | 65,536 | 1 | 25.53 | 76,529,664 B | 50,488,934 / 59,716,403 B | 9,761,193 / 38,891,683 B | not instrumented in baseline | `f8a38b49873588eea0fa55c178b2ad20a05eb7f3eccf55623323405fdec79fe9` |
| After M7, 64 KiB rep 1 | 65,536 | 1 | 37.29 | 70,615,040 B | 50,688,163 / 64,330,137 B | 11,083,448 / 47,018,147 B | 65,536 B | `f8a38b49873588eea0fa55c178b2ad20a05eb7f3eccf55623323405fdec79fe9` |
| After M7, 64 KiB rep 2 | 65,536 | 1 | 32.05 | 83,312,640 B | 50,646,220 / 59,768,832 B | 9,862,905 / 40,632,320 B | 65,536 B | same |
| After M7, 1,000,000 rep 1 | 1,000,000 | 1 | 37.43 | 73,482,240 B | 47,039,119 / 63,512,248 B | 11,324,620 / 39,929,774 B | 1,000,000 B | `85fb2943c331a1ac150ce2e8d8026564b0078d31fda712c461ede9d8527615c0` |
| After M7, 1,000,000 rep 2 | 1,000,000 | 1 | 34.14 | 79,872,000 B | 50,939,822 / 64,728,596 B | 10,789,847 / 45,046,824 B | 1,000,000 B | same |
| After M7, v4 maximum rep 1 | 2,000,000 | 1 | 32.38 | 72,761,344 B | 48,349,839 / 50,384,076 B | 9,907,994 / 41,439,723 B | 2,000,000 B | `4dd44efcf1b36654ae8c2a82f2874a688b746175abf537cffde4559f62b65730` |
| After M7, v4 maximum rep 2 | 2,000,000 | 1 | 34.12 | 91,111,424 B | 82,596,331 / 59,768,832 B | 12,771,655 / 41,471,180 B | 2,000,000 B | same |
| After M7, concurrent 1 MiB pair | 1,000,000 each | 2 overlapping acquisitions | 50.89 group | per process sampled: see JSON | per-container samples: see JSON | per-container samples: see JSON | 2,000,000 B aggregate | same digest per run |

Pre-M7 1,000,000- and 2,000,000-byte runs did not complete: Scrapy's single
`content.read(max+1)` returned only 122,884 and 49,923 bytes respectively,
although gateway evidence had the complete response. They failed before BC06
custody and BC05 publication, so they are not valid performance baselines.
M7 changed the handler to consume bounded chunks and raw-only reference mode;
all three post-change workloads completed with independent receipts and bytes.

The comparable 64 KiB measurements vary in opposite directions across gateway,
worker, RustFS and host-process samples, and total lifecycle latency increased.
They do not establish a measured RSS or throughput reduction. The code path
does establish that raw response size no longer creates a full-body Python
worker response, importer body object, or gateway body `[]byte`; gateway and
importer now move bytes with disk spools and 64 KiB transfer buffers. The
remaining host sample is not isolated enough to quantify that structural
change as a memory improvement. No throughput percentile or production
capacity claim is made.

The first final-envelope run measured zero residual bytes inside each benchmark-owned
acceptance temporary directory after the application returned and before the
benchmark removed that directory. The gateway retained its completed evidence
spool entry by design; it is a durable recovery resource, not temporary residue.

The repeat run (`.local/acquisition-resource-benchmark-20261127T170920Z.json`)
measured two isolated 1 MiB raw acquisitions with overlapping process intervals.
Each independently completed release/build, Work Intake, BC04 attempt, gateway
fetch, protected worker, custody, publication and public artifact verification.
Both had the expected digest, added 1,000,000 bytes to gateway evidence, and
left zero bytes in the benchmark-owned temporary directory before cleanup.
The gateway focused tests separately prove concurrent source reservations and
evidence-read rejection at capacity. This establishes end-to-end concurrency 2
at 1 MiB each; it does not establish concurrency 4/8 or production throughput.

The overlapping pair uses the smaller 64 KiB workload. Two concurrent
2,000,000-byte acquisitions were not run, so the evidence does not establish
large-object concurrency or production throughput.

Machine-readable results are preserved in ignored local evidence:

- Before: `.local/acquisition-resource-benchmark-20261127T144710Z.json`.
- Earlier exploratory run: `.local/acquisition-resource-benchmark-20261127T151615Z.json`.
- Earlier final-envelope run: `.local/acquisition-resource-benchmark-20261127T160100Z.json`.
- Final gateway/concurrency/cleanup run: `.local/acquisition-resource-benchmark-20261127T162204Z.json`.
- Repeated sequential and concurrent 1 MiB run: `.local/acquisition-resource-benchmark-20261127T170920Z.json`.

They are operational evidence, not staged repository artifacts.

## Compatibility, libraries and observability

The live runtime identity moved from
`scrapy-live-http-runtime/1.2.0` to `1.3.0` because raw-only response
representation changed to reference-only. A new BotBuild embeds this exact
runtime identity. Existing BotBuild artifacts are immutable and continue to
carry their prior code and identity. Playbook v4, BotBuild v4, BotRelease v4,
ExecutionCapability, FetchEvidence v1, BC06 receipt and BC05 output contracts
did not change. Structured JSON/HTML, parameterized v3, pagination and redirect
semantics remain body-oriented and unchanged.

No package was extracted. Gateway and BC06 both use Go fixed-buffer transfer,
but their owners and lifecycle differ: gateway controls attempt-bound evidence
spool/retention, BC06 controls independent verified custody spool and object
commit. The trusted acquisition application's HTTP custody adapter remains
owner-local with one compatible raw upload consumer. Evidence and provider
authorization, inventory, retry and publication logic are not in that adapter.

The authenticated resource snapshot is the implemented inspectable signal.
There is no Prometheus metric emission, no high-cardinality labels and no claim
of transfer metrics beyond this snapshot and existing structured component
logs.

## Acceptance matrix T1–T20

| ID | Evidence and validation level | Result |
|---|---|---|
| T1 actual byte-flow inventory | This assessment, source-reviewed across gateway, Scrapy, importer and BC06 | Pass |
| T2 historical profile baseline | `make acquisition-resource-benchmark` before M7; 64 KiB success measured; 1 MB/2 MB fail at worker partial read | Pass with failed larger baseline limitation |
| T3 bounded incremental transfer | Gateway chunked HTTP unit/integration tests; streamed importer and BC06 public upload test; full raw lifecycle | Pass |
| T4 independent evidence/custody integrity | Full raw lifecycle plus independent BC05 read/BC06 streamed digest check at all 3 sizes | Pass |
| T5 raw worker materialization | Runtime unit test proves reference request/empty body; integrated 2 MB path passes | Pass |
| T6 budget hierarchy | Code/config/schema cross-check and exact values in this assessment | Pass; v4 max unchanged |
| T7 aggregate gateway spool | Byte-capacity, entry-capacity, concurrent reservation, duplicate-release and startup-accounting tests; authenticated snapshot exercised in full lifecycle | Pass, single gateway instance only |
| T8 bounded concurrent transfers | Gateway server tests hold two source requests simultaneously and an evidence read while rejecting another; repeat benchmark overlaps two isolated 1 MiB acquisitions and independently verifies both publications | Pass for actual concurrency 2 at 1 MiB end-to-end plus gateway admission; concurrency 4/8 full runs and 2 MiB concurrent files were not measured |
| T9 interrupted transfer recovery | Controlled interrupted-response integration plus gateway incomplete-response and startup-cleanup tests remove `.part`/orphan half-pairs | Pass at controlled/component boundary; no forced process kill or actual container restart was performed |
| T10 tenant-scoped resource/evidence | Existing attempt-bound evidence checks; new resource snapshot contains only global counters; BC06 public API tenant auth remains | Pass |
| T11 safe temporary cleanup | Startup spool cleanup test; importer `TemporaryDirectory` finally cleanup; BC06 owner-local deferred spool cleanup | Pass |
| T12 completed evidence retention | Source-reviewed current owner/lifecycle; completed evidence deliberately retained and bounded | Pass with explicit availability tradeoff; no TTL |
| T13 custody residuals | Existing authenticated tenant-scoped exact artifact metadata/verification and expired-upload recovery; source review | Pass for known operation/artifact inspection; comprehensive residual inventory and cross-context disposition are deferred; no cross-store deletion |
| T14 representative workload | 64 KiB, 1,000,000 bytes, 2,000,000 bytes end-to-end | Pass |
| T15 materially larger workload | The requested 8/32/64 MiB candidates exceed unchanged immutable v4 authorization; no profile was added to benchmark them | **Deferred by explicit scope decision**; current maximum remains 2,000,000 bytes, acquired successfully under T14 |
| T16 before/after measurements | One pre-change 64 KiB baseline; two post-change runs each at 64 KiB, 1 MB and 2 MB; concurrent 1 MiB pair | Pass as sampled evidence; one historical repetition and post-change variation are insufficient to claim RSS or throughput improvement |
| T17 existing raw positive/negative | `make raw-artifact-acquisition-acceptance raw-artifact-negative-acceptance` | Pass on final acquisition code; positive PDF/CSV/ZIP/binary/JSON/reacquisition and all five negative/recovery cases |
| T18 ack-loss/replay | M6 raw negative gate injects lost BC05 acknowledgment, fresh coordinator reconciliation and completed replay | Pass; exact receipt and independent bytes recovered without duplicate publication |
| T19 historical compatibility | Runtime unit/conformance and structured JSON/HTML, pagination, redirect, parameterized v3 regressions | Pass; runtime identity is 1.3.0 for new live builds and old embedded builds remain immutable |
| T20 reproducible CI conformance | Existing CI includes `make check`, raw positive and raw negative; gateway transfer/reservation/concurrency tests are discovered by `make check` Go tests | Pass locally after final checks; no M7 remote CI is claimed |

## Exact commands and remaining limits

Final-tree validation on 2026-11-27:

- `make check`: exit 0; 220 repository unittest cases discovered, 220 passed,
  26 reported skipped (the skip count is not included as passes); Ruff,
  formatting (238 files), docs (86 Markdown files), 16 migration names,
  naming, architecture, Go vet and Go unit targets passed.
- `cd apps/acquisition-gateway && go test -race ./...`: exit 0; gateway
  package passed including entry-capacity and consumer-write-failure slot
  release tests.
- `make acquisition-resource-benchmark ACQUISITION_RESOURCE_BENCHMARK_ARGS='--repetitions 2 --concurrent-size 1000000 --concurrent-count 2'`:
  exit 0; six sequential integrated acquisitions and two overlapping 1 MiB
  acquisitions; report `.local/acquisition-resource-benchmark-20261127T170920Z.json`.
- `make raw-artifact-acquisition-acceptance raw-artifact-negative-acceptance acquisition-live-gateway-container-smoke`:
  exit 0; positive raw profiles, all five raw failure/recovery cases, and
  protected gateway container smoke passed. The publication ACK loss was
  deterministic injection with a fresh coordinator, not a process crash.
- `actionlint .github/workflows/*.yml`, `make docs-check`, and
  `git diff --check && git diff --cached --check`: exit 0.
- Earlier final-tree runs on this same production implementation also passed
  `make artifact-custody-integration` twice against preserved persistent state,
  `make artifact-custody-recovery-tests`, `make acquisition-output-mongo-tests`,
  `make execution-coordination-durable-tests`,
  `make execution-coordination-finalization-tests`, M4 lifecycle, M5 Work
  Intake, protected fixture acquisition, structured JSON/HTML, pagination,
  redirect allow/deny, compiler cross-backend, Scrapy multi-bot, Go build,
  vet, race and vulnerability checks. These were executed before the final
  benchmark/docs/test-only edits; the final `make check` reran unit and static
  gates after those edits.

The existing `ci.yml` required gates run `make check`, raw positive acceptance
and raw negative acceptance; gateway transfer, spool, evidence integrity,
tenant-binding and concurrency tests are included in the Go package discovered
by `make check`. The benchmark remains an explicit developer command, not a
remote CI performance threshold. No remote M7 CI result is claimed before PR.

Limits: no profile above 2,000,000 bytes is authorized or benchmarked; T15 is
deferred by the explicit scope decision. Completed gateway evidence remains
until a safe operator-managed release lifecycle is defined. The finite spool
therefore fails closed at capacity and does not support indefinitely recurring
acquisitions. M9 must define durable, owner-coordinated evidence release
eligibility across current attempts, import/custody recovery, staging,
publication reservations, terminal outcomes and replay before implementing
cleanup. Quota is local single-instance; the concurrent end-to-end run used
two 1 MiB acquisitions, not 2 MiB; concurrency 4/8 was not exercised; the host
importer phase was not isolated for RSS; the two post-change repetitions per
size do not support percentiles; no object-store/provider outage
process-kill matrix was added; no production-scale capacity assertion is made.

T13's remaining operational limitation is that BC06 supports exact
tenant-scoped inspection when an artifact identity is known, but it does not
publish a paginated comprehensive residual inventory with BC05 disposition.
M7 keeps this non-destructive: known custody is retained, unknown disposition
is not treated as absence, and no cross-store garbage collection is attempted.
