# Acquisition platform maturity assessment — 2026-11-25

**Status: assessment for human review.** This records observations, inference, and proposals separately. No ADR is approved and no external source was contacted. The reference repository was read only; local changes were preserved.

## Executive assessment

At main SHA 5f0f8ca, Data Mozaic has controlled live JSON and HTML acquisition, bounded next-URL pagination, durable BC04 attempts/finalization reservation, BC05 MongoDB OutputSets, BC06 PostgreSQL/RustFS custody, an isolated Go gateway, and trusted controlled import/publication. Fixture networking remains disabled. Current implementation also supports policy-authorized planned-window completion and redirects; acceptance evidence varies by scenario and is recorded below.

The platform is not yet mature/reusable. The supported live profile is a deliberately bounded subset of the DSL: JSON/HTML record extraction, bounded pagination and gateway-delegated HTTP. The compiler still emits much of the Scrapy lifecycle into each BotBuild; importer/composition remain in root tooling; SourceRevision, policy and BotRelease are test-installed snapshots, not operational workflows; durable Input intake is missing; raw-only files are not proven end to end. Bodies/results are materialized at multiple boundaries; BC04/BC05 support one OutputSet per Run; BC06 single-part cap is 100 MiB.

Keep ownership: BC15 compiler; BC15 Scrapy framework for engine lifecycle; worker for exact build/runtime verification and process/resource/cancel/report; gateway for source transport policy; trusted acquisition import boundary to be decided in a focused increment; BC06 for logical artifacts/bytes; BC05 for raw outputs/publication; BC04 for attempt/fencing/finalization. A framework boundary may be justified independently of the two-consumer library heuristic.

**Proposed first PR:** move generated Scrapy lifecycle into exact versioned BC15 runtime modules, retain embedded immutable runtime bytes/identity, and reduce generated entrypoint to declarative plan plus thin call. Keep fixture/live profiles distinct and compatible. No trust boundary changes.

## A. Verified Git state

| Check | Result |
|---|---|
| Data Mozaic | `/Volumes/OWC Express 1M2/Develop/data-mozaic`, main. The project owner confirmed that the initially untracked `data-mozaic.zip` was intentionally deleted. No restoration or reconstruction was attempted. |
| Prior PR | #21 `feat/acquisition-live-foundation` merged at 2026-11-25T19:07:19Z. |
| Local/remote main | Both `5f0f8ca5c2df6a90fd97d7de3532673c120785d1`. |
| Consolidation | One worktree reported, synchronized main; no branch created. |
| Reference | `/Volumes/OWC Express 1M2/Develop/capture-architecture` at `27a17f6`; existing modification to `frameworks/scrapy-hub/scrapyhub/crawler/loop.py` and untracked `capture-architecture.zip` preserved. |

The reference loop was read and compared to its repository version. The 2026-11-11 Data Mozaic baseline is historical. Current evidence is in architecture domain/monorepo/libraries/contracts docs, ADRs 018–025, roadmap, consolidation and acquisition assessments. ADRs remain Proposed. No reference deployment/source ran.

### Current live capability and evidence status

“Implementation/test” below means current source, unit/conformance tests, or Make target exists; it is not automatically end-to-end acceptance. “Prior acceptance” refers to executions recorded in `authorized-live-acquisition-2026-11-25.md`. This M0 documentation session has not run live acquisition commands.

| Capability | Current implementation evidence | Previously recorded acceptance evidence / limit |
|---|---|---|
| JSON acquisition | `backend_scrapy_live._supported_graph`, generated live runtime, importer; Make target `authorized-live-json-acquisition`. | Repeated successful JSON run after pagination changes, including trusted import, BC06 digest verification, BC05 publication, BC04 receipt completion and replay. |
| HTML acquisition | `select.html_records`, delegated runtime, controlled HTML importer; target `authorized-live-html-acquisition`. | Successful controlled HTML run and later HTML regression after pagination changes. |
| Bounded next-URL pagination | `pagination.bounded/v1`, page/request budgets, visited-URL loop detection, harness `--case pagination`. | Successful controlled two-page run. Import checked page order, evidence/digests, next-link transition, provenance and natural termination. |
| Natural termination | Runtime reports `natural_end` only when no further link exists; importer verifies no residual final links. | Proven in the recorded successful two-page pagination run. |
| Planned-window completion | `completion_mode: bounded_window`; runtime reports `planned_page_limit`; trusted importer requires authorized mode, page count and in-scope continuation. Compiler/importer tests and target `authorized-live-pagination-planned-acquisition` exist. | No successful durable planned-window execution is recorded in the prior assessment. Treat as implemented/tested, not previously E2E accepted; M1 must run the target. |
| Authorized redirects | Gateway policy can revalidate every redirect hop; target `authorized-live-redirect-acquisition` exists. | Prior assessment records gateway redirect code/tests but no successful integrated redirect run. M1 must run the controlled target. |
| Redirect denial/budget | Gateway denies out-of-scope target or exhausted redirect budget; denial and budget-exhaustion targets exist. | Unit/harness negative cases exist, but prior assessment records no durable BC04→BC06/BC05 outcome. M1 must run both and confirm no publication. |
| Gateway operation admission | Worker delegates source requests; gateway calls BC04 per hop; capability binds tenant/run/attempt/generation/release/source/policy. | JSON/HTML/pagination acceptances and gateway container smoke provide integration evidence; tests cover current-attempt rejection/restart semantics. |
| Failure and recovery | Harness scenarios cover oversized/slow/malformed, redirect denial/budget, cancellation, gateway restart, custody interruption, partial stage and publication-ack loss. Dedicated Make targets exist for named cases; a generic controlled-scenario target accepts additional harness cases. | Prior record includes pagination cancellation, stale/cross-tenant/unknown-fetch/digest rejection and gateway isolation. It says not all redirect/quota/restart scenarios were run durably. Ack-loss/custody/stage targets have no prior acceptance record here. M1 should run the regression set. |

Do not infer raw-only arbitrary files, unbounded traversal, offset/cursor pagination, browser execution or external-source authorization from these capabilities.

## B. Reference architecture maturity

### Scrapy Hub

`frameworks/scrapy-hub/pyproject.toml` declares Poetry/Scrapy; `scrapyhub/main.py` configures logging, CrawlerProcess and CLI; `crawler/settings.py` contains proxy toggles. `crawler/crawler.py` is empty. Repository `crawler/loop.py` contains historical/commented implementation and the checkout has a local modification. `tests/test_hello.py::test_hello` is `pass`. Queue polling, schema registration, heartbeats, retry and output are intent, not tested framework behavior. Runtime schema mutation conflicts with immutable contract binding. Proxy/queue behavior cannot replace BC04, gateway, trusted importer, BC06/05 or fencing. Adapt host-owned lifecycle with narrow extension points; do not call it mature.

### Python libraries

Reference packages are Poetry/Nx generated, have broad Python ranges and placeholder READMEs. Package metadata does not establish consumers. Python package names below are relative to the reference repository's `libs/python/` directory.

| Package | Actual responsibility/consumer | Evidence and coupling | Recommendation |
|---|---|---|---|
| `capture/stats-collector` | Dummy/StatsD collectors; Scrapy loop | Narrow, placeholder test, StatsD-specific | Defer until operational consumer; metrics are not durable heartbeat |
| `capture/rmq-queue` | pika consume/publish/ack; loop | Broker lifecycle; integration test, recovery unproved | Reject default; no messaging need |
| `capture/py-stream` | Source/output plus status/heartbeat/end markers; loop | 238 LOC and 239 test LOC; mixes control/data | Adapt progress only; preserve DM boundaries below |
| `capture/py-proxy` | Proxy provider selection | requests/backoff/OAuth/discovery | Reject; gateway enforces each hop, env proxy is bypassable |
| `support/py-request` | Requests/auth/retry; proxy/services | 143 LOC, 619 test LOC; business retry leakage risk | Defer; caller owns business retry |
| `support/py-log` | JSON logger | Thin/global, minimal test | Adapt `mozaic-observability` narrowly |
| `support/py-status` | Generic errors; stream | Placeholder tests, broad meanings | Reject universal errors |
| `support/py-debug` | Request inspection | 74 LOC, 94 tests; redaction unclear | Reject runtime; bounded/redacted diagnostics |
| `support/py-sd` | Service discovery | Sparse/infrastructure-bound | Defer; explicit endpoints suffice |
| `support/args-parser` | CLI helpers | App flags leak into support | Keep in composition root |
| `services/py-repository` | 9-line base class | No domain ownership | Reject generic repository |
| `services/py-schema` | Schema create/read client | Mutable runtime coupling | Adapt read-only immutable resolver |
| `services/py-output` | Output/schema client | Wrapper stack; empty schema identity/version possible | Reject model; future BC05 client only |
| `testlib/support-py-repository` | Generic test base | Small inheritance helper | Defer; local fixtures/contract vectors |

Reference direction layers support → service wrappers (py-output → py-schema → py-repository → request/log), scattering retry/schema/error semantics. Data Mozaic must not collapse `CaptureOutput` (BC05 data), `FetchEvidence` (trusted gateway fact), `ExecutionReport` (worker report interpreted by BC04), `OutputSet` (BC05 completeness), `CaptureRun/CaptureAttempt` (BC04 lifecycle), and `PublicationReceipt` (BC05 durable publication) into one stream. A local ResultSink/progress API is possible; wire meanings remain separate.

### Go libraries and services

Reference Go modules include HTTP server/router/middleware, Mongo/Rabbit clients, event dispatcher, UUID/MD5/JWT, JSON Schema, UDP metrics and DTOs. Tests vary; wrappers can hide transaction/ack semantics; MD5 is not a content-integrity digest. Do not reproduce Nx/go.work mechanically. Data Mozaic has real Go apps/acquisition-gateway and artifact-custody; keep drivers in owner adapters, DTOs from approved contracts, BC06 internals private.

| Reference Go library path | API/consumers | Evidence and recommendation |
|---|---|---|
| `libs/golang/server/http-web-server/webserver` | HTTP server setup used by service mains | Tests exist. Compare lifecycle/shutdown with `net/http`; adapt locally, no shared server contract. |
| `libs/golang/server/http-router` | Custom router/tree | Router/tree tests exist. Use only if routing behavior beats standard/mature options; no domain API. |
| `libs/golang/server/http-middleware` and `http-support` | HTTP middleware and support helpers | Tests exist, adoption varies. Keep at service edge. |
| `libs/golang/clients/resources/go-mongo/client` | Mongo client/session construction; repository modules depend on it | Tests exist. BC05 owns Mongo transaction/session policy; wrapper must not hide retry/session lifecycle. |
| `libs/golang/clients/resources/go-rabbitmq/client` | Consumer/notifier | Unit and compose integration tests. Ack/reconnect semantics are broker-specific; reject without a durable consumer. |
| `libs/golang/capture/events` | Event dispatcher interfaces/mock | Unit tests. Dispatch is not outbox or delivery guarantee. |
| `libs/golang/capture/id/go-uuid`, `go-md5` | ID helpers | Tests exist. UUID mechanics are replaceable; reject MD5 for integrity. |
| `libs/golang/capture/jwt` | JWT helpers | Tests exist. Do not adopt without matching issuer/audience/key-rotation contract. |
| `libs/golang/capture/json-schema` | JSON Schema validator and schema files | Tests exist; useful technique, but contract identity/version remains owner semantics. |
| `libs/golang/clients/udp/statsdig` | UDP metric sampling | Sampler tests exist; not a platform observability contract. |
| `libs/golang/capture/dtos/*`, `libs/golang/clients/apis/*` | Service DTOs and clients for input/output/schema/config/storage | Mostly service-shaped modules; generate bindings only from approved schemas and only for a Go consumer. |

Reference Go services have independent `go.mod` files while `go.work` and Nx coordinate the repository. This is a toolchain choice, not evidence Data Mozaic needs a workspace. Consumer edges must be verified from imports/manifests, not directory co-location.

| Service | Observed implementation | Gap and Data Mozaic decision |
|---|---|---|
| `services/core/input-api` | Entity/use case, HTTP, DB repo, schema cache, dispatch/handler/tests | Create then dispatch is not outbox; adapt idempotent intake |
| `services/core/input-listener` | Queue consumer | Inbox/replay/commit-before-ack unproved; no broker by analogy |
| `services/core/output-api` | Entity/use case/repo/cache/schema client | Empty schema ID/version path; BC05 immutable set is stronger |
| `services/core/output-listener` | Minimal main/config | Operational lifecycle unproved |
| `services/core/schema-api` | Mutable create/update/lookup | Local immutable resolver; no bot mutation |
| `services/core/config-api` | Generic config API/DB | Not source/policy/release authority |
| `services/core/router-api` | Routing skeleton | Durable frontier/leases/fairness unproved; excluded |
| `services/core/runtime-api` | Runtime/config/health skeleton | No trusted resolution; pin exact build/image |
| `services/capture/storage-api` | File entity/use cases/local repo/HTTP/tests | Lacks BC06 intent/digest/reconciliation; no second storage service |
| `services/capture/proxy-api` | Proxy loaders/pool/scheduler/feedback | No source/path enforcement or direct-egress prevention; reject |

Reference event dispatch does not prove outbox, delivery, idempotency, cross-store ACID or recovery.

## C. Comparative decision matrix

| Capability | Reference | Data Mozaic | Reusable principle / incompatibility / owner | Priority and validation |
|---|---|---|---|---|
| Engine | main.py, commented loop, empty crawler | live compiler/runtime/child | Host lifecycle; reference loop not trust-safe; BC15 | P1 identity/conformance/cancel |
| Input | input-api/listener | BC04 durable Run, no BC13 | Validate before dispatch; BC13/04 | P1 idempotency/crash/replay |
| Schema | mutable schema-api | draft contracts/BC05 payload ref | Immutable ID/version/digest; contract owner | P1 digest/compat vectors |
| Output | output-api/stream | BC05 outputs/sets/grants/receipts | Keep publication/fencing; BC05 | lost-ack/replay |
| Storage | generic file API | BC06 identity/intent/digest/RustFS | Keep custody; BC06 | digest/stream/receipt |
| Network | proxy-api | capability gateway | Reject proxy; gateway | direct-egress/redirect/rebind/quota |
| Events | dispatcher/Rabbit | durable outbox facts | no broker without consumer | relay/inbox/replay if justified |
| Shared modules | Nx tree | uv/Go, L2 bot-build/observability | selective extraction | no Nx; named consumers |

## D. Data Mozaic dependency and ownership audit

| Unit | Actual responsibility |
|---|---|
| `apps/scout` BC14 | PostgreSQL sessions/evidence/findings/proposals, MCP/HTTP; not source/release approval |
| `apps/collector-engineering` BC15 | Playbook v2/Typed IR, reference/fixture/live compilers, runtime bundle/identity |
| `apps/workers/scrapy-collector` | Catalog/build selection, mozaic-bot-build verification, child/cancel/cleanup/report; host+verifier+adapter |
| `apps/acquisition-gateway` | Go operation admission, DNS/address checks, delegated HTTP, quota/evidence; sole live egress |
| `apps/execution-coordination` BC04 | PostgreSQL Run/Attempt/lease/generation/finalization, signed authority and reconciliation |
| `apps/acquisition-output` BC05 | Mongo stage/seal/publish OutputSets, receipts/outbox/read |
| `apps/artifact-custody` BC06 | Go API, PostgreSQL intents/receipts, RustFS bytes, bounded upload/digest |
| `contracts` and migrations | Draft/versioned contracts and owner migrations; BC05 Mongo ownership |
| `libs/python/bot-build` | L2 path/file/hash verifier used by BC15+worker, not publisher/auth |
| `libs/python/observability` | L2 event/correlation primitives, one Scout consumer |
| `tooling/tests/examples` | Composition, recovery, importer, E2E and test oracles |

Dependency rule: contract → app/SDK edge; app → own domain and consumer ports; adapters implement ports; deployables compose. No cross-context private model/table/migration/repository. Static checks miss dynamic/generated/subprocess edges. Persistence: BC04 PostgreSQL; BC06 PostgreSQL+RustFS; BC05 MongoDB; BC14 PostgreSQL. Worker has no direct persistence. Keep uv and app Go modules.

### Compiler, framework, worker

`playbook_v2.py` validates/lowers Typed IR; `backend_scrapy_fixture.py`/`backend_scrapy_live.py` compile; `reference_runtime.py`, `scrapy_fixture_runtime.py`, `scrapy_live_runtime.py`, runtime identity modules hold operators/transport/binding. `backend_scrapy_live.generate_bot` emits Spider lifecycle, handler checks, request graph, evidence checks, extraction, pagination, in-memory records/evidence/documents, output and cleanup. It is deterministic but duplicates lifecycle.

| Responsibility | Owner |
|---|---|
| Syntax/types/capability and deterministic build | BC15 compiler |
| Source/policy/release admission | authority owners composed by BC04; compilation is not approval |
| Scrapy lifecycle/operators/budgets/result sink | versioned BC15 framework |
| Fixture/gateway engine adapter, no fallback | BC15 |
| Catalog/build/runtime/image verification, child/resource/cancel/deadline/report | worker |
| Runtime result contract and runtime interpretation semantics | BC15 |
| Trusted result import application boundary | Focused decision: compare BC04, BC05, BC15 and narrow composition boundary; BC04 retains execution/finalization authority, BC05 CaptureOutput/OutputSet ownership, BC06 custody |
| URL/DNS/hop/quota/evidence | gateway |

Worker files under `src/scrapy_collector/catalog/`, `execution/`, `__main__.py`, and `settings.py` verify selected build then launch child. `__main__.py` reads all records.jsonl with read_bytes; report cap 64 KB. Fixture uses Docker network none; live uses GatewayDownloadHandler/guard. Keep selection/isolation/report in worker; never pass authority keys to framework.

BotBuild options: A current generated lifecycle + exact embedded runtime is deterministic but duplicates code; preserve during transition. B thin entrypoint + exact embedded package shares fixes/conformance; recommended. C immutable IR interpreter reduces build but makes IR an executable contract; defer until second engine evidence. D arbitrary Python weakens trust/reproducibility; reject. Bind plan/compiler/backend, runtime files/hashes, lock/profile, capability and worker compatibility. Historical builds remain immutable/profile-bound; no mutable latest or silent conversion.

## E. Trusted importer, library, and SDK boundaries

Observed line counts: fixture_acquisition.py 629; live_acquisition_import.py 631; run_durable_fixture_acquisition.py 687; run_authorized_live_json.py 1,355.

| File | Responsibility | Target |
|---|---|---|
| `tooling/fixture_acquisition.py` | Strict/duplicate JSON, safe paths, bundle/evidence/schema, stable IDs, BC06 intent/upload, Record/Artifact/Composite stage/seal | Import behavior is in tooling; decide application owner in a focused increment across BC04/05/15/narrow composition. Fixture oracle stays harness. |
| `tooling/live_acquisition_import.py` | Trusted gateway evidence, report/pagination, controlled mapping/output; imports fixture-private helpers/types | P0 coupling issue; focus ownership review and remove private cross-script coupling. Runtime result contract/interpretation may remain BC15; BC04/05/06 ownership does not move. Controlled mapping remains test oracle. |
| `tooling/run_durable_fixture_acquisition.py` | BC04/worker/BC05/BC06 composition, recovery/assertions | Composition root/test, no service |
| `tooling/run_authorized_live_json.py` | Test authority/source/gateway/DB/build/worker/import, faults/assertions | Controlled E2E; test keys/oracles remain test-only |

Importer flow: verify build/report/current attempt; retrieve trusted evidence and check digest/size/representation; validate source/build/runtime; resolve contract; stable IDs; BC06 intent/upload/receipt; stage one BC05 set; seal; BC04 reservation; exact grant; reconcile receipt; complete Run. Worker never marks success. Stable operation IDs. Ownership remains a focused decision across BC04 (execution/finalization authority), BC05 (CaptureOutput/OutputSet), BC15 (runtime result contract/interpretation) and a narrow application composition boundary. No microservice just to relocate code.

Library maturity follows existing L0–L4: L0 private default; L1 candidate; L2 owner/API/named consumers/independent tests; L3 governed compatibility/conformance/security; L4 external support. bot-build and observability are L2 internal experimental.

| Candidate | Owner/API/consumer/exclusion | Maturity and validation |
|---|---|---|
| BC15 Scrapy runtime | BC15; compiler and worker integrate differently; context/plan/profile/transport/result sink; excludes auth/BC04/custody/publication | Governed framework boundary may be warranted even if consumers do not fit a library extraction rule. Decide distribution from ownership, API, exact identity, compatibility and actual integration; no speculative scaffold. |
| Strict JSON | Contract/tooling; worker/gateway/importer if duplicated; bounded duplicate-key parse only | L1; Unicode/duplicate/size/number/trailing vectors |
| Content integrity | BC15/BC06 edges; streaming SHA256/size/representation, no auth/signature | L1; cross-language vectors first |
| Result bundle reader | BC15 contract; worker producer/importer consumer; exact inventory/bounded JSONL, no trust | L1; version and consumer conformance |
| Bounded I/O | Different gateway/worker/BC06 budgets | no universal library absent identical lifecycle and second consumer |
| BC06 client | BC06 contract; Python importer; streamed intent/upload/idempotency/receipt, no provider state/auth/retry | L1; public contract, timeout/cancel, fake+local integration, logical refs |
| BC05 client | BC05 contract; importer/raw consumer; stage/seal/read, no persistence | L1; visibility/tenant/manifest/receipt; wait for public API |
| BC04 SDK | no independent network consumer | defer; do not force HTTP |
| Contract resolver | contract owner; compiler/intake/importer; read-only ID/version/digest | L1 local index; mismatch/compat/offline tests |
| Generic Postgres/errors/observability | no second identical consumer | reject/defer; semantics remain owner-local |

Create `libs/<language>/<name>` only with owner and compatible consumer semantics. Extraction may happen inside a functional PR when it materially improves the capability; do not require a library-only milestone. No common/utils/shared-core, driver wrappers, Nx or empty roots. Preserve uv, Go, Make, CI and L0–L4 governance.

## F. Source, contract, release, and Input lifecycle

BC04 `trusted_live_authority.py` and migrations 002-authority-snapshot-registry.sql / 003-acquisition-operation-admissions.sql implement signed snapshots, anti-rollback, durable admission and attempt-bound capability. Controlled acceptance constructs source/policy/release and temporary key; not product BC01/11 lifecycle.

BC01 minimum: source identity, immutable revision/digest, owner, origins/locale/input contract, approved capabilities/policy refs, eligibility/revocation/lookup. BC11 owns policy; BC15 compatibility; BC04 composes. Keep modules/contracts + signed snapshots until service operations are justified. Revocation prevents new admissions, not already sent requests.

Contracts distinguish wire, input, Playbook, BotBuild, result, source payload, future dataset. `contracts/**/draft/**` is explicit; compare copies under collector-engineering by digest. Resolve immutable ID/version/SHA256 at compile/admit/import, propagate through release/Input/result/OutputSet. No runtime schema mutation or universal registry.

BotRelease draft exists; operational approval/revocation missing. Bind Playbook revision, compiler/backend, BotBuild/runtime/image, source/policy, input/output contract digests, capabilities and reviewer evidence. Compile is candidate only; independent authority approves; BC04 admits exact release. No release service.

BC13 durable input intake missing. Keep distinct Logical Input; immutable Input revision; Submission key+canonical digest; Dispatch; BC04 CaptureRun; fenced CaptureAttempt; gateway operation; retry (same Run/new attempt); reprocess (new Run/lineage); new observation. Validate tenant/source/release/input contract; idempotent receipt; exact release; create Run. Direct local call is valid. Add BC13-owned Postgres outbox only for actual async need: same-transaction, lease/reclaim/backoff, inbox/dedupe, ack after durable Run, replay/quarantine. No Rabbit/Kafka by analogy. BC13 owns Inputs, BC04 Runs. No frontier/pipeline DAG.

## G. Agent-to-Playbook and raw acquisition

BC14 Scout evidence/findings/proposals and authoring tools exist; BC15 compiles v2; worker runs verified build. Gaps: operational SourceRevision/scope approval; durable proposal-to-reviewed revision; contract resolver; release approval; Input intake; owned importer.

~~~mermaid
flowchart LR
 S[BC01 SourceRevision] --> SC[Approved Scout scope]
 SC --> E[Trusted evidence]
 E --> P[PlaybookProposal]
 P --> R[Human-reviewed revision]
 R --> C[BC15 compile]
 C --> B[Verified BotBuild/runtime]
 B --> A[Independent release approval]
 A --> BR[BotRelease]
 BR --> I[Validated Input]
 I --> X[BC04 Run/current attempt]
 X --> W[Protected worker/framework]
 W --> G[Authorized gateway]
 G --> SRC[Controlled source]
 W --> IM[Trusted importer]
 G -->|evidence| IM
 IM --> C6[BC06 custody]
 IM --> C5[BC05 raw OutputSet]
 C5 --> RC[Independent consumer]
~~~

Agent acts only in approved Scout scope; evidence digested; proposal is data, not code; human/authority reviews; compiler fails closed; production run needs no agent. Controlled conformance before separately authorized external exploration. Agent cannot widen scope or self-approve.

Gateway captures decoded bounded HTTP entity bytes, not TLS wire bytes. FetchEvidence is trusted hop fact; ArtifactOutput references BC06 logical identity/receipt; RecordOutput is schema-bound JSON; CompositeOutput links records/artifacts. Not normalized entities/datasets. Preserve PostgreSQL for BC04/BC06 inventory, RustFS/S3-compatible BC06 bytes, MongoDB BC05 outputs. No binary BSON, physical URI or signed URL identity. Consumers read published sets and fetch by logical reference.

Raw-only CSV/XML/PDF/ZIP/binary is not end-to-end proven. Live compiler supports JSON/HTML record selectors, bounded next-URL pagination and record/raw references; recorded controlled acceptance proves JSON, HTML and natural pagination. Fixture HTML/pagination evidence is separate and is not used to claim live support. BC06 byte support alone is insufficient. Add raw-fetch IR without parser, bounded framework handoff, gateway digest/representation evidence, importer verification, BC06 intent/upload, BC05 ArtifactOutput, BC04 grant/receipt. Composite may combine record+artifact in one set; no bot custody keys.

Materialization: gateway buffers bounded body/evidence; spool exists but downstream reads bytes; Scrapy body materialized; generated bot retains records/evidence/documents/raw bodies; worker reads entire records.jsonl; importer accumulates bundles/pages/records; BC05 stages incrementally but upstream retains; BC06 copies to 0600 limited temp, hashes, single conditional PutObject. BC06 cap 100 MiB/concurrency; multipart absent. Component bounds are not streaming end to end.

No invented throughput targets. Benchmark controlled increasing body/count/concurrency for RSS/temp/copies/first-stage/cleanup/cancel/recovery. Bound per-response/run bytes, requests/pages/records/files/deadline, temp, memory, concurrency from trusted policy. Stream SHA256, bounded chunks, incremental JSONL/schema, backpressure/receipts. Gateway restart retains quota and invalidates old capability. One OutputSet publishes after full validation/seal. Multipart only if measured and recovery designed.

## H. Target map and repository

~~~mermaid
flowchart TB
 BC01[Source] --> BC14[Scout]
 BC14 --> BC15[Compiler/framework]
 CR[Contract index] --> BC15
 BC11[Policy] --> BR[Release approval]
 BC15 --> BR
 BR --> BC13[Work Intake]
 CR --> BC13
 BC13 --> BC04[Run/Attempt]
 BC04 --> WK[Worker]
 BC15 --> WK
 WK --> GW[Gateway]
 GW --> SRC[Source]
 GW --> BC04
 WK --> IM[Importer]
 GW --> IM
 IM --> C6[BC06 Postgres + RustFS]
 IM --> C5[BC05 Mongo]
 BC04 -->|grant/receipt| C5
 C5 --> CON[Raw consumer]
 C6 --> CON
~~~

Implemented vertically: BC14, BC15 compiler, BC04/05/06, gateway, worker. Partial BC01/11 lifecycle, BC08 resolver, BC15 framework. Trusted importer exists in tooling; its application owner is open. Missing BC13. BC16 frontier and BC09/10 transformations are not prerequisites.

Keep apps/, apps/workers/, contracts/, owner migrations/, deployments/, tooling/, tests/, examples/, libs/python/ and app Go modules. First runtime consolidation may establish an internal BC15 package boundary. An independent frameworks/scrapy package is justified when it provides coherent ownership, API, runtime identity, compatibility, dependency/test boundaries and actual compiler/worker integration; the two-consumer heuristic must not block that review. No speculative or cosmetic scaffold. Preserve uv/Go/Make/CI/checks; no Nx, empty roots or cross-app private imports.

## I. Ordered roadmap and acceptance

P0 correctness/security/ownership; P1 pre-source capability; P2 concrete operations; P3 future scale. This assessment originally stopped before implementation. The PR1 runtime consolidation is now implemented locally on `feat/scrapy-runtime-framework`; its evidence and remaining gaps are recorded below. Later roadmap increments remain unstarted.

| PR | Objective, owner/scope, exclusions | Acceptance, ops, rollback, remaining limit |
|---|---|---|
| 1 BC15 runtime P1 | Move lifecycle to exact runtime; backend/identity/worker. No gateway/source UI/Playwright/arbitrary Python; preserve old profiles. | Exact files/hash; fixture network none; live gateway-only; mismatch/capability denied; cancel/timeout cleanup; fixture and currently supported live JSON/HTML/natural/planned pagination/redirect behavior preserved; no oracle. Catalog rollback. |
| 2 Trusted importer P0/P1 | Focused ownership review of BC04/05/15/narrow composition, then move only boundary needed for the vertical. No service/oracle in prod/stream redesign. | Bad JSON/path/symlink/size/digest/stale attempt/forged evidence/schema rejected; replay stable; crash after custody/stage/seal/publish reconciles. Runtime result contract stays BC15; BC04/05/06 ownership stays intact. |
| 3 Contract resolver P1 | Local immutable index binds compiler/build/intake/import. No online editing/bot registration/service. | ID/version/digest/compat vectors; unknown/changed/incompatible fail; offline deterministic; old builds pinned. |
| 4 Source/Release P1 | BC01/11/15 owner-named PostgreSQL state and operator composition + BC04 snapshot ops. No microservice/full engine/external source. M4 implementation is now local on `feat/source-release-lifecycle`; see the linked evidence. | Controlled approval/install/admission/publication/revoke flow passed locally; no remote M4 CI. Shared build distribution, production identity/key operations and broader persistence/failure matrix remain limitations. |
| 5 Work Intake P1 | BC13 schema/Postgres/port to BC04. No broker/frontier/pipeline. | Same key+intent same Run; changed conflicts; tenant/contracts; crash recovery; retry same Run/new attempt; reprocess new lineage; duplicate completion. Submit/inspect/cancel/reprocess. |
| 6 Raw Artifact/Composite P1 | IR/schema, fixture/live, gateway evidence, importer, BC06/05. No parser/multipart/direct bot credential. | Controlled file/binary digest/size, redirect/caps, unauthorized media/size, cleanup; composite one set/logical retrieval. Keep 100 MiB pending evidence. |
| 7 Incremental transfer conditional | Streaming optimization follows observed resource behavior. Explicit safe memory/disk/response/aggregate bounds remain mandatory in earlier increments. No arbitrary SLO/multipart absent evidence. | Benchmark before redesign; if needed, verify RSS/temp caps, backpressure, incremental validation, no partial publish, restart quota and crash/cancel cleanup; old profile rollback. |
| 8 Integrated conformance P1 | Scout→contract/release/Input/BC04/worker/gateway/importer/BC06/05/consumer; controlled source. | A JSON; B HTML pagination; C raw file; D composite; E replay/cancel/crash/recovery. Exact binding/fencing, no direct egress, evidence/digest/custody, sealed set/grant/receipt, published-only consumer. Record commands/digests/faults. |
| 9 Readiness human gate | Threat/ops/key/retention review; no source implementation. | No P0; PR8 passes; contracts/owners/budgets/recovery approved; separate scoped authorization. |

PR1–6 finite capabilities; PR7 conditional, PR8 integration, PR9 human decision. PR2/3 overlap only if resolver prerequisite. No library-only speculative PR.

Operational local flow: existing infra/readiness, resolve controlled source/contracts, author/validate, compile/verify, independent approval, submit Input, inspect Run/attempt/gateway/import, read published set, fetch logical artifact, reconcile. Reuse Make/tooling/apps; no universal CLI or cross-worktree config.

**Finite pre-source DoD:** PR1–6 reviewed/integrated; PR8 A–E passes; source/release/input/output owner+version+digest bound; exact build/runtime; fencing/restart quota/trusted evidence/BC06 custody/BC05 receipt fault-tested; independent consumer reads published raw data/artifact; explicit safe bounds are configured and tested; submit/inspect/cancel/retry/reprocess/reconcile documented; no P0 and human readiness approval. Streaming is required only if measured behavior needs it. HA, broker, browser, pipeline, parsers, arbitrary scale and real source are not required.

## J. Invariants, rejected options, open decisions

Preserve fixture network none; live no direct route/gateway only; gateway validates URL/DNS/hops/source/policy/method/path/deadline/quotas at dial; no secrets in logs; signed anti-rollback authority; exact source/policy/release/build/runtime; attempt fencing; quota continuity on restart; trusted evidence; BC06 logical identity/intent/digest/receipt; BC05 invisible staging/immutable manifest/grant/idempotency; BC04 reservation blocks replacement until receipt; cancellation/deadline/late-result semantics; strict untrusted input and cleanup. No distributed ACID/exactly-once claim.

Reject Nx, reference tree replication, generic Rabbit/Kafka, runtime schema mutation, compile-as-approval, bot self-registration, env/CONNECT enforcement, arbitrary Python, mutable latest, unjustified services, MinIO beside RustFS, binary Mongo, signed URL identity, Playwright/frontier/Kubernetes HA/parsers/lakehouse/broad rewrite/shared-core.

Open decisions: approver and key custody/rotation for BC01/11/15; local contract index/digest approval; first BC13 caller/async need; raw workload vs 100 MiB; evidence/artifact retention/orphan policy; SDK vs local composition; evidence for independent framework distribution. None blocks PR1.

## Validation

- Previous assessment session: `make check` passed with 225 tests and 26 environment-dependent skips. This is historical evidence, not a rerun in the current session.
- M0 documentation session: `make check` exited 0. Lock, Ruff lint/format, Markdown links/fences, migrations, naming, architecture, Go vet/tests and repository Python tests passed. The run reported 225 passed and 26 skipped because `SCOUT_TEST_DATABASE_URL` was unset. No live acquisition acceptance command was run in M0.
- `git diff --check` and `uv run --locked python tooling/check_docs.py` will be recorded again after this final document edit and before branch publication.
- Read-only repository/PR/worktree checks, current docs/ADRs, reference package/service inventory, and targeted compiler/worker/gateway/importer/BC05/BC06 inspection. No source, deployment, migration, destructive operation, commit, push, PR or ADR status change has occurred yet. Reference was not executed.

The project owner confirmed that the initially untracked `data-mozaic.zip` was intentionally deleted. It was not restored or recreated.

## M1 local implementation evidence — PR1 runtime consolidation

The M0 assessment was integrated in PR #22 at `d38bb38bece832bbd14436b20dfc6e74682c449b` before this feature branch was created. This update records the local M1 implementation; it is not evidence of an M1 commit, PR, merge or remote CI run.

BC15 now owns the private `collector_engineering.scrapy_framework` API (`run_fixture` and `run_live`). Fixture/live compiler output is an immutable plan plus a small entrypoint; the bundle includes framework and adapters in the BotBuild runtime file inventory. New runtime identity is `collector-runtime/1.1.0`; old `1.0.0` identities/toolchains remain explicitly allowlisted and historical build bytes are not rewritten. No standalone framework package, library, service, SDK or plugin mechanism was added. The worker still verifies catalog build identity, runtime-backed entrypoints and protected process results. Fixture handlers remain offline; live requests remain gateway delegated. BC04/05/06 authority remains unchanged.

Generated source responsibility changed materially: compiler code no longer emits Scrapy Spider classes, callbacks, settings, handler-table checks, process startup, output writing or cleanup. It emits only the closed acquisition plan, exact runtime identity and a `main()` call into the embedded runtime. The framework owns those shared lifecycle behaviors while fixture and live transports remain separate. No code was moved from the trusted importer, no test oracle entered runtime, and no streaming redesign was attempted.

Measured comparison against base `d38bb38` regenerated the historical generator functions with the same example IRs and reconstructed runtime identities from the exact historical Git blobs. Fixture HTML pagination generated source fell from 14,286 bytes/187 lines to 5,321 bytes/11 lines; live HTML generated source fell from 12,043 bytes/111 lines to 3,885 bytes/11 lines. First-party embedded runtime bytes per build changed from 40,602 (historical fixture) and 39,714 (historical live) to 74,535 for each new profile, since each new BotBuild now embeds the framework and both profile adapters. These are source and embedded-module byte counts, not compressed image size or throughput/memory claims. No performance improvement is claimed.

Local verification completed before final documentation validation:

- Focused Scrapy backend/runtime/compiler tests: 15 tests passed.
- Focused framework initialization-cleanup tests: 2 tests passed; fixture and live temporary roots were removed when runtime-context initialization was injected to fail.
- `make collector-cross-backend-conformance`: passed JSON/HTML parity, pagination, tamper/missing/partial-output failure handling, provenance/order, and network denial.
- `make scrapy-collector-multibot-conformance`: passed protected multi-bot execution and callback failure/timeout cases.
- `make execution-coordination-protected-e2e`: passed callback failure, cancellation, crash/recovery, healthy-after-crash, HTML/JSON/pagination and timeout cases.
- `make acquisition-live-gateway-container-smoke`: passed gateway-only route enforcement and unauthorized-origin/direct-route denial.
- `make authorized-live-json-acquisition`: passed end-to-end trusted import, custody and publication.
- Live controlled acceptance targets for HTML, natural pagination, planned pagination, authorized redirect, redirect denial and redirect-budget exhaustion exited successfully.
- `make authorized-live-publication-ack-loss`, `make authorized-live-pagination-cancellation`, `make authorized-live-custody-interruption` and `make authorized-live-partial-stage-interruption` exited successfully. The interruption cases left no published partial output and surfaced durable reconciliation state.
- Separate `make authorized-live-scenario CASE=...` invocations for `gateway-restart`, `oversized`, `slow`, `malformed`, `invalid-output`, `interrupted-body` and `pagination-incomplete` exited successfully. These are expected terminal failure/recovery scenarios, not successful acquisitions.
- The first combined generic-scenario attempt encountered a transient Docker TLS handshake timeout fetching the pinned Distroless manifest from GCR; the separate retry succeeded. Make deduplicates repeated instances of the same target, so each `CASE` was invoked separately.
- Final `make check` exited 0: lock, Ruff check/format, documentation links/fences (74 Markdown files), nine migration names, naming, architecture, Go vet/tests, library/worker/context tests and repository tests passed; the final Python suite reported 225 passed and 26 skipped because `SCOUT_TEST_DATABASE_URL` was unset. The two added framework cleanup tests also passed through the focused command and BC15 test discovery.
- Final `make collector-cross-backend-conformance scrapy-collector-multibot-conformance execution-coordination-protected-e2e acquisition-live-gateway-container-smoke` exited 0 after the cleanup change. Cross-backend cases preserved independent provenance/order and network denial; protected E2E retained failure/cancel/crash/timeout plus healthy recovery; live smoke remained gateway-only with direct/unauthorized routes denied.
- Final `make docs-check naming-check architecture-check lint format-check` and `git diff --check` exited 0. No M1 remote CI exists because this branch is unpublished.

This section is the local M1 implementation snapshot captured before delivery. M1 was subsequently reviewed and integrated through PR #23: reviewed head `5d04dca9ecee66729279d9eab715079d915384bb`, all seven remote checks passed, and squash-integrated main SHA `f9e53d325a8167b380d31a66d925c89ab05a4fa0`. No external source was implemented or accessed. Importer ownership/migration, contract/source/release lifecycle, Work Intake, raw-only artifact acquisition, measured streaming work and an integrated independent raw consumer remain later roadmap items.

## M3/M4 delivery state — 2026-11-26

M3 immutable payload contract resolution was reviewed at PR #25 head
`3442371d47f9704c49450c5616a1ccb4fa9b87a9`, passed all seven required remote
checks and integrated into `main` at `8c16167d92a1a40eefcfe711151881507e9e5911`.
See the [M4 implementation assessment](source-release-lifecycle-2026-11-25.md)
for M3 delivery evidence and the local M4 implementation.

M4 adds `apps/acquisition-lifecycle` as an in-process operator composition.
BC01, BC11 and BC15 owner-named schemas hold immutable revision/approval state;
BC08 contract resolution remains repository-versioned; the existing BC04
Ed25519/anti-rollback registry accepts signed generations. A complete controlled
live JSON acquisition used this lifecycle for source, policy, Playbook, build,
candidate, approval and authority, then exercised existing protected worker,
gateway, importer, BC06 custody, BC05 publication and BC04 terminal result. A
subsequent revocation snapshot preserved history and rejected new resolution of
the release. M4 remains local and unpublished. The operator identity model is a
local allowlist, build availability is a verified local path, and no production
RBAC, multi-host artifact distribution, Source/Release service, Scout workflow,
Work Intake or real-source acquisition is claimed.
