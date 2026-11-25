# ADR 023 — BC06 Artifact Custody service and local object storage

- Status: Proposed
- Date: 2026-11-24
- Owner: BC06 Artifact Custody
- Scope: first operational BC06 foundation and local S3-compatible development profile

## Context

BC06 owns logical artifact identity, content integrity, object custody, inventory, access enforcement, retention references and custody receipts. BC05 owns CaptureOutput and consumes its published `ArtifactRef`/custody verification port. BC04 owns attempt state and finalization authorization; BC09 owns dataset publication. A service boundary is justified for bounded byte streaming and independent storage credentials, but domain types remain private to BC06 and contracts remain language-neutral.

The repository is Python-first and has no Go deployable yet. BC06 is the first independently operated infrastructure service with concurrent streaming I/O, request cancellation/deadlines and cross-language consumers. A Go module is a proportionate test of the multilingual boundary; measured performance superiority is not assumed.

The intended MinIO community distribution is no longer a supportable default. The upstream `minio/minio` repository is archived and read-only, the upstream states its AGPLv3 community code is end-of-life and no longer receives fixes, and the last source release is dated 2025-10-15. The inspected Quay image request returned 401, so it is not a reproducible anonymous local dependency in this environment. MinIO AIStor is commercially licensed. This assessment does not make a legal determination about AGPL obligations.

## Decision proposed

1. Implement the initial BC06 service in Go under `apps/artifact-custody`, using a pinned supported Go toolchain and owner-specific `internal/` packages. Use Go standard library HTTP, pgx for PostgreSQL, and AWS SDK for Go v2 S3 APIs. No Go shared library is created.
2. Keep the BC06 domain and application independent of PostgreSQL, S3, HTTP, and DTOs. Persist intent, logical artifact identity, availability, operation receipt and BC06 outbox in an owner-specific PostgreSQL schema. Stream bytes through a bounded S3 adapter. Do not hold a PostgreSQL transaction open during object transfer.
3. Use RustFS `1.0.5` as the locally validated S3-compatible development/test implementation, pinned by OCI index digest. Its index contains Linux amd64 and arm64 manifests. The RustFS project identifies its source as Apache-2.0 and documents S3 compatibility. Release `1.0.6` became available on 2026-11-15 after this local volume was first provisioned on `1.0.5`; retain the exact tested pin for this evidence and review the upgrade before a future validation that includes object backup/restore and rollback. BC06's production object storage provider remains an explicit deployment decision; S3 compatibility alone does not establish complete feature parity.
4. Keep the public contract draft and immutable at `contracts/artifact-custody/draft/v1/`. Go models remain private; the Python BC05 adapter consumes HTTP contracts and never reaches into BC06 storage or inventory.
5. Enable only an explicit local/test bearer-token authorization adapter in this foundation. Production use fails closed until BC11 authorization is integrated. Retention is represented by policy references; this service does not automatically delete objects.

## Alternatives considered

### Python implementation

Rejected for the first deployable, not for all future BC06 components. Python could stream with bounded readers and its current ecosystem is mature. A second independently operated infrastructure service would add a distinct runtime and resource lifecycle, while Go provides a small statically built service and tests the required Python/Go contract boundary. There is no benchmark evidence that Go is faster for this workload.

### MinIO community image/source build

Rejected as the default local distribution because the community source is archived/EOL, security maintenance ended, the official release stream is frozen, and the inspected Quay manifest endpoint required authentication. Building that frozen source ourselves would make the container technically reproducible but would not make the software supported or receive security fixes.

### MinIO AIStor

Not selected for the repository's default developer setup. It is commercially licensed and depends on product access/license provisioning not present in this repository. Reconsider if the organization explicitly adopts the commercial product and its deployment terms.

### Local filesystem or mock object store

Rejected as the only integration environment because it would not exercise the S3 API, credentials or object metadata used by the adapter.

### RustFS

Selected only as a local/test S3-compatible implementation. It is not a transparent replacement for all MinIO behavior. The adapter tests the implemented subset: bucket/object operations, conditional single-part writes, streamed reads, content length and checksum metadata. The current configured upload limit is 100 MiB and multipart is not implemented; larger artifacts require a separately justified change. Any additional S3 behavior requires a separate conformance test. Production object-store choice remains open.

## Consequences and risks

- Go introduces toolchain, build, race and vulnerability gates while Python remains the existing platform foundation.
- RustFS is a newer project and its API/operational behavior must be tested against the service's used S3 subset; its Apache license does not establish operational support guarantees.
- MinIO is not removed from Data Mozaic as an architectural option; this local deployment proposal is explicit and remains Proposed pending human review.
- The local service is not production-ready: BC11 authorization, TLS, multi-node database/object storage, disaster recovery policy and BC04 publication fencing are outside this increment. The local bearer policy is explicitly test-only.
- Cross-store object/SQL consistency is recovered by durable intent identities and bounded reconciliation, not distributed transactions.

## Validation criteria

- `go fmt`, `go vet`, unit tests and `go test -race` pass on the pinned Go toolchain; Linux/amd64 container builds and local macOS/arm64 development work.
- Draft JSON fixtures independently round-trip in Go and Python with strict unknown-field/duplicate handling, Unicode, absent/null, timestamps, SHA-256 and interoperable integer limits.
- Real PostgreSQL and the pinned local S3-compatible endpoint exercise intent reservation, streaming limits, digest/size checks, retry, reconciliation, restart, receipt/outbox atomicity and tenant scoping.
- A local logical PostgreSQL backup/restore smoke verifies BC06 inventory, operation, outbox and migration fingerprints in a temporary isolated schema. It does not back up or restore RustFS object bytes; object-store backup/restore remains unvalidated.
- This ADR remains Proposed until a human approves the Go and local S3-compatible deployment direction.

## Sources reviewed

- [MinIO upstream repository status](https://github.com/minio/minio) (archived/read-only as of 2026-04-25).
- [RustFS releases](https://github.com/RustFS/RustFS/releases) and [RustFS license](https://github.com/rustfs/rustfs/blob/main/LICENSE).
- [Go release history](https://go.dev/doc/devel/release); the module uses the Go 1.27.4 patch release.
