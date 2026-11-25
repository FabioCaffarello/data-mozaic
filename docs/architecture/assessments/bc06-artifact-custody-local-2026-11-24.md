# BC06 Artifact Custody local implementation evidence — 2026-11-24

This is implementation evidence for the BC06 foundation branch, not production readiness or acceptance of ADR 023. The MongoDB persistence PR #17 was integrated at `138e90bdcaf7bf7c2972a1efccc9fff824de9494` before this branch began.

## BC05 MongoDB review gate

PR [#17](https://github.com/FabioCaffarello/data-mozaic/pull/17) is merged, non-draft, with no unresolved comments or formal review decision recorded by GitHub. Its recorded Quality Gate, BC05 MongoDB Integration Gate and Integration Security Gate all concluded success for the PR head `5def724dd7066535f1875ed25b6ba69aa6ece16d`; the squash merge on `main` is `138e90bdcaf7bf7c2972a1efccc9fff824de9494`. A subsequent local run of the actual replica-set suite passed all 16 tests, including independent-client stage/stage, stage/seal, seal/seal, publish/publish, publish/abort, replay, rollback, drift and published-query cases. The review found no material correctness defect in its database transaction boundary, revision-fenced stage/seal protocol, immutable membership, streamed manifest verification or publication/outbox write.

One capability limitation remains: the BC05 application port exposes `stage_output` for a single output and does not expose atomic bounded chunk staging. Each output therefore uses its own transaction. Chunk-size comparisons (1/10/100/500) are not supported by the current contract; the existing benchmark measures individual stages and same-set versus independent-set concurrency. A future chunk API should be evaluated as a separate BC05 increment because it changes operation identity, atomicity, byte/count budgets and failure/replay semantics. No BC05 API was changed in this BC06 increment.

## Local stack observed

- Developer host: macOS 15.6, arm64; Docker 28.5.1; Go 1.27.4; Python via locked uv workspace.
- BC06 Go runtime: Linux container, non-root, Compose limits 768 MiB and 2 CPUs. Upload concurrency is capped at four, configured byte limit is 100 MiB per object and the local temporary filesystem is bounded to 420 MiB.
- Object store: RustFS 1.0.5, image pinned to OCI index digest `sha256:b81d16a90ee25bd9511280127ca1471788c6e874cf0f0da759645f46b2dc1d74`; local validation used that exact image and preserved named volume. RustFS 1.0.6 is now available, so the tested 1.0.5 pin is behind the newest release. I did not upgrade a live persistent volume without a validated object backup/restore and rollback plan; review release compatibility before changing the pin. The upstream identifies the project as Apache-2.0 and provides 1.0.6 as a release, but that does not imply production support guarantees.
- PostgreSQL uses the existing local PostgreSQL container and volume, with BC06-specific `artifact_custody` schema and separate `bc06_app` / `bc06_owner` roles. MongoDB used the preserved BC05 replica-set environment and volume. No volume removal command was run.
- After the recorded local workloads, BC06 inventory relation size was 112 kB for 13 intents; the outbox contained 10 events. RustFS `/data` occupied 223 MiB. This is an absolute observed footprint, not an isolated growth measurement: the volume already contained prior synthetic E2E and benchmark objects.

## Interoperability and restart evidence

`make artifact-custody-integration` passed against real local PostgreSQL, RustFS and BC05 MongoDB. It wrote 18,000 bytes labeled synthetic, restarted the BC06 service container, verified the same durable receipt after restart, replayed the same operation, rejected changed intent and digest mismatch, staged/sealed/published the existing draft ArtifactOutput fixture through BC05, read it through a fresh MongoDB client, and downloaded bytes from BC06 with matching SHA-256. This does not test service-process crashes at each PostgreSQL/object-store failure window or cross-store BC04 fencing.

## Single-transfer synthetic benchmark

The optional benchmark was run twice; the latest run is recorded below. It used one sequential transfer per size, a generated disk-backed byte file, 64 KiB HTTP read/send chunks, and a fresh artifact identity each time. The download hash matched the declared digest. Timings are a single sample per size on a local Docker Desktop stack, without percentile or concurrent-load characterization.

| Size | Upload | Upload rate | Download | Download rate |
| ---: | ---: | ---: | ---: | ---: |
| 1 MiB | 0.0640 s | 15.61 MiB/s | 0.0368 s | 27.16 MiB/s |
| 10 MiB | 0.2154 s | 46.43 MiB/s | 0.1046 s | 95.61 MiB/s |
| 100 MiB | 1.3890 s | 72.00 MiB/s | 0.8403 s | 119.01 MiB/s |

The Python benchmark process peak RSS was 26,984,448 bytes and `tracemalloc` peak was 284,187 bytes on macOS 15.6 arm64. These figures exclude the Go service, PostgreSQL and RustFS; they are not an application memory bound measurement. No controlled concurrent-transfer, Go RSS, PostgreSQL growth delta, throughput percentile or reconciliation-cost benchmark was run.

## PostgreSQL backup/restore smoke

`make artifact-custody-backup-restore` ran `pg_dump` for the BC06 schema, restored it into a temporary randomly named schema in the same local PostgreSQL database, and compared row counts and deterministic fingerprints for inventory/receipts, operation receipts, outbox events and migration history. It passed and removed only its own temporary schema. The dump did not include RustFS object bytes; no object-store backup/restore test was performed. The live object data stayed in the original named volume.

## BC05 MongoDB workload note

The integrated BC05 staging API exposes one-output `stage_output`; it does not expose bounded atomic chunk staging, so benchmarking chunk sizes 1/10/100/500 against BC05 is not supported by its public application port. Its existing MongoDB benchmark measures individual stage transactions and concurrent workers sharing or separating OutputSets. Add a bounded chunk use case only as a separate BC05 increment with explicit atomicity, operation-identity, encoded-size and manifest-budget rules; this BC06 branch does not change that API.

## Limits

- The local bearer policy is only `local`/`test`. BC11 authorization, TLS, production tenancy and retention execution are absent.
- Object storage is S3-compatible RustFS, not MinIO. No claim is made about complete S3 compatibility or production provider support.
- The adapter uses conditional single-part `PutObject` and a bounded temporary spool. Multipart and artifact sizes above 100 MiB are unsupported.
- PostgreSQL inventory and object storage do not share a transaction. The integration validates one end-to-end success and process restart, not every failure window.
- Outbox facts are durable and inspectable; no event relay or delivery guarantee exists.
- BC06 local leases do not prove current attempt ownership in PostgreSQL-owned BC04. ADR 022 remains Proposed.

## Hardening review amendment — 2026-11-24

This section records review work performed after the original local assessment. It supersedes no earlier measurements; prior statements remain historical observations from their recorded commands.

### Findings and corrections

- Upload lease expiry was calculated on the Go host while acceptance and expiry checks used PostgreSQL `clock_timestamp()`. This made claims sensitive to host/database clock skew. Claim and reconciliation lease deadlines, and custody confirmation timestamps, now use PostgreSQL time. Application logic still uses generation, attempt ID and random lease token checks; these are BC06 upload fences only and do not authorize BC05 publication against BC04.
- `CompleteUpload` could persist an adapter-supplied location not matching the immutable claim key. It now rejects empty provider/bucket/key and any key other than the claimed generation's key. S3 reads now reject non-S3 provider identities and retain a version ID returned by `HeadObject`.
- Runtime configuration previously bounded each artifact and the Compose semaphore separately but did not assert the aggregate relationship. Startup now rejects configurations where `max_bytes × max_concurrent` exceeds the configured temp budget. The default is 420 MiB for four 100 MiB transfers; the Compose tmpfs is also 420 MiB. The HTTP download handler has a ten-minute request deadline. Unit tests cover accepted and rejected aggregate budgets.
- Existing reconciliation columns were not updated. Reconciliation now marks the attempt `pending`, `consistent` or `inconsistent` and records its last check time. Missing objects remain pending; divergent metadata or bytes are inconsistent and do not produce a receipt.
- Reconciliation classification initially depended on an attempt-ID prefix. Because the attempt header is caller supplied, a normal upload could spoof that prefix. The claim now carries an explicit adapter-internal reconciliation flag; the real-store test confirms a prefixed caller attempt cannot change reconciliation state.
- The S3 adapter verifies provider identity and the claim key, while conditional `PutObject` prevents a stale writer from replacing an already-created generation key. A later generation uses a new key. A stale write to an unreferenced old key can still leave an orphan; there is no destructive orphan cleanup.

### Real-store recovery and object-copy evidence

`make artifact-custody-recovery-tests` passed against the running PostgreSQL and RustFS services after the corrections. One real-store integration test uses unique synthetic tenant, operation, artifact and object identities; it does not delete prior state. It exercised:

- An object written successfully before inventory confirmation, then an expired PostgreSQL lease and reconciliation. The object was re-read and hashed, the inventory/receipt/outbox completed, replay returned the same receipt, and the original stale claim could not confirm again.
- An expired intent with no object. Reconciliation left it pending, persisted `reconciliation_state=pending` and `last_reconciled_at`, and did not confirm custody.
- A real object with metadata digest divergent from the intent. Reconciliation classified it inconsistent and `VerifyArtifact` refused confirmation.
- Two independent PostgreSQL clients concurrently uploading identical bytes for one operation. The eventual receipts matched; the losing call retried only `ErrInProgress` within a two-second bounded test loop.
- A conditional write to an existing object key with different bytes. RustFS returned `PreconditionFailed`; the original object size/digest remained. Application credentials could not write outside the BC06 prefix.
- Copy to distinct synthetic backup and restore keys in the same RustFS bucket, followed by inventory mapping to the restore key and a BC06 application-layer read whose size and SHA-256 matched.

This evidence exercises process-independent database/object behavior, not a forced process kill during an in-flight multipart transfer (multipart is unsupported), an independent storage failure domain, full bucket snapshot consistency, or production point-in-time recovery. The object copy shares the same RustFS instance and volume. The PostgreSQL smoke separately restores the BC06 schema to a temporary schema and compares fingerprints. Together these are logical local recovery checks, not an independent backup system.

### Updated bounded benchmark

`make artifact-custody-benchmark` passed on macOS 15.6 arm64 using Docker Desktop 28.5.1, Go 1.27.4, RustFS 1.0.5, 64 KiB transfer chunks and two sequential samples per size, then four concurrent 10 MiB round trips. These synthetic values are individual samples, not percentiles or service objectives.

| Size | Upload samples (seconds; MiB/s) | Download samples (seconds; MiB/s) |
| ---: | --- | --- |
| 1 MiB | 0.0990 / 10.10; 0.0263 / 38.00 | 0.0255 / 39.26; 0.0264 / 37.83 |
| 10 MiB | 0.1734 / 57.66; 0.1572 / 63.62 | 0.0983 / 101.69; 0.0998 / 100.24 |
| 100 MiB | 1.4901 / 67.11; 1.2816 / 78.03 | 0.9220 / 108.46; 0.6696 / 149.34 |

Four concurrent 10 MiB upload/download round trips completed in 0.8015 seconds aggregate wall time (49.91 MiB/s aggregate based on uploaded bytes; all download digests matched). The Python process peak RSS was 28,016,640 bytes and `tracemalloc` peak was 913,822 bytes. Docker stats sampled the BC06 container four times and observed 28,227,665 bytes maximum container memory. This is container memory, not Go heap/RSS attribution. Tmpfs peak use, PostgreSQL per-operation latency, RustFS memory and CPU were not directly measured. The 420 MiB mount and startup budget establish configured bounds, not a sampled peak.

### Provider security and support status

The tested service is still pinned to RustFS 1.0.5 by digest. The upstream repository lists 1.0.6 as the newer release and says only the latest version is supported for security updates. The current upstream advisories reviewed in this gate identify affected versions at or below 1.0.1 (or earlier); the 1.0.5 pin is outside those published ranges. Runtime tests validated the S3 behaviors BC06 uses: conditional single-part writes, immediate head/read after a committed write, object metadata, size/digest verification, copy/read-back and least-privilege prefix denial. This is not a complete S3 conformance certification. I did not upgrade the existing named volume: the newer release has not been exercised with an isolated volume clone, rollback or full object inventory restore. Consequently the old 1.0.5 pin is a support-lag risk to resolve through a separate reviewed upgrade rehearsal before relying on RustFS operationally beyond local development.

The current GitHub upstream marks MinIO's repository archived/read-only, while RustFS publishes Apache-2.0 licensing and multi-architecture release assets. Those facts informed retaining the tested S3-compatible provider rather than substituting an unavailable historical MinIO image. RustFS remains a local-development provider; production provider support and security guarantees are not established.

### Commands and limits

- `make artifact-custody-recovery-tests`: passed against actual PostgreSQL and RustFS, one integration test.
- `make artifact-custody-benchmark`: passed, two samples each at 1/10/100 MiB and a four-transfer concurrent round trip.
- `make artifact-custody-backup-restore`: still required for the PostgreSQL isolated-schema restore and is not conflated with independent-volume recovery.
- The cross-store BC04 finalization reservation/grant remains unresolved; ADR 022 stays Proposed. No distributed fencing claim follows from these BC06-only tests.
