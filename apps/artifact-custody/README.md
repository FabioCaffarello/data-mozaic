# BC06 Artifact Custody (draft foundation)

This Go deployable owns logical artifact identity, byte integrity, BC06's PostgreSQL inventory, storage locations, custody receipts, scoped local access and the custody outbox. Its HTTP JSON contracts are versioned and remain draft. BC05 consumes them through its Python HTTP adapter; no BC05 code imports this module's private Go types or queries its database.

## Local setup

From the repository root:

```sh
make artifact-custody-setup
make db-up
make artifact-custody-db-bootstrap
make artifact-custody-up
make artifact-custody-status
make artifact-custody-integration
make artifact-custody-recovery-tests
make artifact-custody-backup-restore
```

`make artifact-custody-setup` creates a local ignored environment file and preserves it on subsequent runs. The setup/bootstrap steps require the existing local PostgreSQL Compose service. Object storage is a pinned RustFS S3-compatible development service; the application receives a bucket-restricted service account separate from the RustFS provisioning credentials. The named volume survives ordinary `make artifact-custody-down`.

The integration target uses the local BC05 MongoDB environment if present, or the preserved MongoDB worktree environment. It uploads synthetic bytes, restarts BC06, verifies the durable receipt, stages and publishes an ArtifactOutput through MongoDB, then downloads and hashes the bytes. It does not access a public source or run a collector.

The backup target restores a logical BC06 PostgreSQL schema into a temporary random schema and compares inventory, operation, outbox and migration fingerprints. `pg_dump` and `psql` run inside the existing pinned PostgreSQL Compose container to keep client and server versions aligned. It drops only the temporary schema it created. The real-store recovery test also copies a synthetic object to unique backup and restore keys in the same RustFS bucket, verifies its digest and reads the restored key through BC06. The database and object checks are separate synthetic exercises, not one coordinated snapshot, independent-volume backup or disaster-recovery procedure.

Run the bounded synthetic transfer benchmark with:

```sh
make artifact-custody-benchmark
```

Default sizes are 1, 10 and 100 MiB with two samples each, followed by four concurrent 10 MiB upload/download round trips. To use a smaller local workload, set `BC06_BENCH_SIZES_MIB=1,10`. The benchmark uses disk-backed input and 64 KiB transfer chunks and samples the BC06 container memory via Docker stats. It does not directly sample tmpfs use, Go heap, PostgreSQL memory or RustFS memory.

## Boundaries and limits

- `internal/domain` contains private invariant-bearing types and strict contract decoding.
- `internal/application` owns consumer-facing ports and custody use cases.
- PostgreSQL, S3-compatible storage and HTTP are private adapters under `internal/adapters`.
- `cmd` contains process composition roots; migrations are BC06-owned.
- Uploads are bounded by the local 100 MiB configuration and concurrency semaphore. The current object adapter spools to a size-limited `0600` temporary file, hashes while copying, then issues one conditional S3 `PutObject`. Multipart is not implemented. Increasing the cap requires revisiting the temporary storage budget and adding tested multipart completion/abort and recovery behavior.
- The local bearer policy is test-only. Startup rejects modes other than `local` and `test`; this is not production authorization, TLS, HA, or a legal-retention implementation.
- PostgreSQL and object storage do not share a transaction. A durable intent and generation-bound upload lease plus bounded reconciliation recover incomplete writes. Custody is confirmed only after the object is verified and receipt/inventory/outbox commit together.
- The outbox is durable and inspectable; no relay or delivery guarantee is implemented.
- BC06-local leases do not authorize BC05 publication against PostgreSQL-owned BC04 attempts. ADR 022 remains Proposed and unresolved.

## Maintenance

Use the root targets `artifact-custody-go-format-check`, `artifact-custody-go-vet`, `artifact-custody-go-test`, `artifact-custody-go-race`, `artifact-custody-go-build` and `artifact-custody-go-vuln`. The module is pinned to Go 1.27.4 in `go.mod`; container builds target Linux and local development supports the Go toolchain's host architecture.

Infrastructure helpers such as S3 client lifecycle, local configuration validation, transaction/error classification and transport instrumentation are L1 extraction candidates only. They remain BC06-private until a second concrete consumer exists. Domain repository, migration, receipt, reconciliation and access semantics remain context-owned.
