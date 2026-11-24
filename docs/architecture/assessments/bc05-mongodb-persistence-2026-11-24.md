# BC05 MongoDB persistence increment — implementation evidence

**Status:** implementation evidence on `feat/acquisition-output-mongodb`; not merged. ADR 022 remains **Proposed**. This note amends the historical architecture baseline with evidence from the MongoDB increment and does not revise the accepted domain ownership decisions.

## Scope and boundaries

BC05 keeps CaptureOutput, OutputSet, operation idempotency, acquisition-output publication, receipts, queries and the publication outbox. BC04 still owns run/attempt state and authorization. BC06 still owns artifact identity and custody. BC09 still owns dataset publication. The adapter implements only BC05-owned ports; `domain.py`, `application.py` and `ports.py` do not import PyMongo or BSON.

The Python adapter is local to `apps/acquisition-output`. `MongoResources` owns a synchronous PyMongo client; `MongoOutputStore` maps private domain values to BSON and implements BC05 repository/publication/query ports. Public JSON contracts, private Python domain values, and BSON have separate mappings. The BSON `wire` member contains the v1 CaptureOutput projection, while query and integrity fields are duplicated at documented top-level paths and checked against that projection when read.

## Collections and indexes

| Collection | Owner and document granularity | Key indexes and policy |
|---|---|---|
| `output_sets` | One bounded OutputSet snapshot, revision, counters, immutable manifest digest and (after publish) receipt. No member array. | Unique `(tenant_id, output_set_id)`; all state writes increment `revision`. |
| `capture_outputs` | One CaptureOutput observation per document; payload remains bounded inside its fixed wire envelope. Logical artifact IDs/digests are retained; artifact bytes are never stored here. | Unique `(tenant_id, output_set_id, output_id)`, used for deterministic keyset membership reads and manifest ordering. |
| `output_operations` | One scoped command identity, intent digest and original bounded result. | Unique `(tenant_id, operation_id)`; resolves replay after application restart. |
| `output_outbox` | One bounded `OutputSetPublished/v1` event reference per publication, in `pending` state. It does not include acquired records. | Unique `event_id`; legacy pending index and tenant-scoped `(tenant_id, state, created_at_us, event_id)` index. |
| `bc05_schema_migrations` | One applied BC05 schema version and checksum. | Unique `version`. |

Schema validators and indexes are provisioned by BC05 migration versions 1 and 2. Provisioning is repeatable and fails closed on validator, policy, expected index or checksum drift. It does not drop collections, indexes or data. The local application credential has `readWrite` only on `bc05_outputs`; root provisioning and metrics use a separate ignored local credential. No bot selects a collection name.

The normal member key paths (`tenant_id`, `output_set_id`, `output_id`, `observation_id`, `operation_id`, `intent_digest`) are verified against actual BSON documents. Exact domain microseconds for lifecycle timestamps use integer `*_at_us` fields; `observed_at` remains the RFC3339 string in the wire projection. Payload numbers remain within the foundation I-JSON safe-integer/finite-number rules. BSON encode size is checked before insertion, in addition to domain JSON budgets. Round-trip mapping recomputes intent digest and compares canonical byte count; no millisecond `datetime` truncation is used for domain timestamps.

## Transaction and recovery semantics

Mutations use PyMongo sessions with snapshot read concern, majority write concern, primary routing, retryable transaction callbacks, an operation timeout and a bounded commit timeout. Semantic output, operation, publication, and event identities are computed outside retryable callbacks. The callback performs database writes only.

- Create writes the set snapshot and operation receipt together.
- Stage inserts one bounded member, conditionally increments the common OutputSet revision/counters, and writes the scoped operation receipt together. Stage and seal therefore contend on the set document across independent clients; a Python process lock is not used.
- Seal streams the member commitment projection sorted by `(output_id, operation_id)` through a Mongo cursor with bounded batches, computes the existing manifest v1 digest, and conditionally seals against the scanned revision. A moved revision restarts the bounded scan. Application snapshots, set inspections, replay and publication receipt reads do not load members.
- Publish atomically changes the sealed OutputSet, stores one durable receipt, inserts one pending event and records the operation receipt. Publish/abort contend on the same set revision. A repeated publish returns the original receipt and does not add another event.
- Abort/reject are terminal transitions; staged, sealed, aborted and rejected sets are not returned by published queries.

The manifest verification is streaming in application memory: O(batch size + one member commitment + fixed manifest fields), not O(member count). The v1 digest bytes are preserved. Transaction size is bounded per stage operation to one CaptureOutput plus its receipt. A set's common revision document is a deliberate serialization point; the benchmark includes its contention cost.

Integration tests use real independent PyMongo clients. The “lost acknowledgement” test injects a lost response after the transaction committed, then confirms replay through a fresh application call. This is useful replay evidence, but it is not a network fault-injection test. MongoDB-local state does not fence PostgreSQL-owned current attempts.

## Publication and query guarantees

The durable outbox event has stable identity, BC05 source/type, aggregate and tenant identity, publication microsecond time, correlation/causation references, bounded payload and `pending` state. There is no dispatcher or broker. Recording a pending outbox row is not delivery and does not claim at-least-once delivery. Change Streams are not used as a replacement for domain events.

Published-output queries first require a matching `(tenant_id, output_set_id, state=published)` gate and then use ascending `output_id` keyset pagination. The opaque base64 cursor is bound to tenant and set filters and has bounded page size; it is not an authorization token. Membership within a published set is immutable, so its pages remain stable. There is no cross-set snapshot cursor or snapshot claim.

The unresolved BC04↔BC05 issue remains exactly the check-then-publish race documented in Proposed ADR 022. A later integration must introduce a BC04-owned durable finalization reservation or generation-bound publication grant, with explicit cancellation, expiration, restart, conflict and lost-ack semantics. This adapter accepts only locally scoped fixture/test publication commands; no worker-facing production authorization endpoint is provided.

## Local operator workflow

Local topology is a single MongoDB 8.0.38 replica-set node with the official pinned image, named persistent volume, keyfile authentication, loopback-only host binding, two-CPU/two-GiB Mongo limit, separate application/provisioning credentials, and no public listener. This supports transaction and Change Stream development, not high availability. Host PyMongo uses `127.0.0.1` with `directConnection=true`; container-side discovery was checked against the replica-set name `rs0` from the Compose network. No TLS is configured for this loopback-only local profile.

Run `make acquisition-output-mongo-setup` once to create ignored local credentials and keyfile. Normal operator commands are:

```text
make acquisition-output-mongo-up
make acquisition-output-mongo-connectivity
make acquisition-output-mongo-provision
make acquisition-output-mongo-tests
make acquisition-output-mongo-example
make acquisition-output-mongo-backup-restore
make acquisition-output-mongo-benchmark
make acquisition-output-mongo-down
```

Ordinary shutdown preserves `data-mozaic-bc05-mongodb-data`; no `down -v` target exists. Backup/restore uses the image's `mongodump`/`mongorestore`, restores to a random isolated namespace and leaves that namespace available for inspection. The smoke compares output/set identities and manifest digests, receipts, operation identities, event references, validators and indexes. This is a local consistency smoke, not a production backup policy, RPO or RTO commitment. Abandoned staging sets are inspected through bounded `output_sets` records (`state=staging`, age from `created_at_us`) and their counters/member rows; operators may retry or issue an explicit abort command. There is no automatic cleanup or retention policy.

## Observed validation and synthetic measurements

Environment observed in this worktree: macOS 15.6, arm64 host, 10 logical CPUs; Docker Linux arm64; MongoDB 8.0.38 limited to 2 CPUs/2 GiB; PyMongo 4.20.0; synthetic record around 1.55 KiB logical BSON storage per output; one output per transaction; 10,000 output maximum per OutputSet. A second benchmark run produced:

| Workload | Throughput | p50 / p95 / p99 stage latency | Seal / read | Notes |
|---|---:|---:|---:|---|
| Sequential, 100 outputs | 106.3 outputs/s | 8.34 / 16.58 / 19.75 ms | seal 9.2 ms; 7,832 outputs/s paged read | 49,087-byte `tracemalloc` peak; collection extent did not grow at this size. |
| Sequential, 10,000 outputs | 179.7 outputs/s | 5.23 / 7.63 / 13.89 ms | seal 109.5 ms; 10,233 outputs/s paged read | 456,297-byte `tracemalloc` peak; capture output collection grew 2,928,640 storage bytes and 2,617,344 index bytes; logical data 15,538,890 bytes. |
| Four writers, one set, 400 outputs | 256.3 outputs/s | 3.95 / 48.24 / 219.01 ms | — | Tail latency shows shared-set contention. |
| Four writers, four sets, 400 outputs | 729.4 outputs/s | 5.19 / 7.37 / 9.99 ms | — | Independent set state reduces this local contention. |
| Ten sequential sets, 100,000 outputs total | 130.9 outputs/s end-to-end | Per-set figures emitted by the harness | 763.9 s total, including ten seals/publications and ten paginated reads | Each set stayed at 10,000 members; 100,020 transactions started/committed, zero aborts and zero reported retry counters. |
| Four writers after the 100k workload, one set, 400 outputs | 215.5 outputs/s | 4.62 / 32.10 / 125.70 ms | — | Same-set contention measured with the larger synthetic database state present. |
| Four writers after the 100k workload, four sets, 400 outputs | 549.7 outputs/s | 6.85 / 11.85 / 20.13 ms | — | Independent set case under the same larger database state. |

The 10,000-output case recorded 10,002 started/committed transactions (stage, seal, publish; the set creation precedes the measurement), zero aborts and zero server retry counters. The 100,000 case ran successfully with the same zero abort/retry observations. The 100k run used the existing local volume with prior synthetic records present; it is not a clean-volume benchmark. Concurrent results varied with database state, as shown in the two runs. Measurements are local synthetic baselines, not production capacity claims. The in-memory test double, unit mapper tests and real MongoDB adapter evidence are distinct. Peak memory is Python `tracemalloc`, not total process RSS. Index growth and transaction counters are MongoDB-reported values.

### Chunk staging review

The current BC05 port exposes `stage(output, intent_digest, custody)` and the application exposes `stage_output(output)`. Each CaptureOutput has its own scoped operation identity and durable receipt; MongoDB commits one member, its operation receipt and one OutputSet revision update per transaction. There is no atomic multi-output staging command, so chunk sizes 10, 100 and 500 are not supported workloads and were not benchmarked. The single-output measurements above are not a chunk comparison. Adding chunk staging would require specifying replay behavior for a set of per-output operations, all-or-nothing custody confirmation, per-chunk byte/time limits and transaction retry semantics across both adapters. That is a consumer-facing BC05 application/port capability, not an adapter-only bulk-write optimization. No current consumer requires it, so this increment keeps the existing contract; introduce a bounded chunk use case with independently reviewed semantics when a real consumer demonstrates the need.

## Library maturity

All Mongo-specific client lifecycle, config validation, BSON mapping, transaction control, provisioning and error classification remain L0 BC05 implementation. L1 extraction candidates only after a second real consumer: generic PyMongo client lifecycle/pool configuration, technical config validation, bounded transaction retries, driver error classification and instrumentation. BC05 mapping, repository behavior, OutputSet lifecycle, query visibility, idempotency and publication remain private context responsibilities. No `mozaic-mongodb` wrapper is introduced.

## Remaining gates

- The 100,000-output run is synthetic on one local machine; it does not establish production capacity or HA.
- No live acquisition, source exploration, BC04 durable coordination, distributed finalization grant, BC06 artifact custody or MinIO integration.
- No external outbox delivery mechanism; event state remains pending for inspection.
- No TLS for this local-only database profile; production credentials, network policy and server topology require a separate deployment decision.
- Provisioning and backup/restore were tested locally on MongoDB 8.0.38 only.
