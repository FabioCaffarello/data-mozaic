# Trusted Acquisition Application

This internal Python application composes the verified BC15 result semantics with current execution context from BC04, output use cases from BC05, and artifact bytes through the BC06 public HTTP contract. It is an in-process application boundary, not a bounded context, service, scheduler, worker, or alternative finalization protocol.

## Ownership

- BC15 remains the owner of the compiled acquisition plan, runtime/build compatibility, extraction semantics, and worker result contract.
- The acquisition gateway remains authoritative for live fetch authorization, transport policy, and `FetchEvidence`. Fixture evidence is resolved from the verified fixture bundle.
- BC04 remains the owner of current attempt/generation, execution state, finalization reservation, cancellation, and terminal completion.
- BC05 remains the owner of `CaptureOutput`, `OutputSet`, stage/seal/publish, and `PublicationReceipt`.
- BC06 remains the owner of logical artifact identity, content inventory, custody, and custody receipt.

The application validates one protected result bundle against a verified immutable BotBuild and trusted evidence, then delegates custody, staging, sealing, finalization, and reconciliation to those owners. It has no persistence, signing key, database adapter, retry queue, or provider domain aggregate.

## Interfaces

`TrustedAcquisitionApplication` exposes fixture/live verification and import use cases, plus thin delegation to BC04's existing finalization and reconciliation operations. Consumer-owned ports in `ports.py` cover custody and finalization. `CurrentExecutionReader` is the context read port. The initial composition uses BC04's durable repository interface; it does not query PostgreSQL tables directly.

`VerifiedResultBundle` is a bounded application result, not a public cross-context DTO or proof of authorization. `trusted_bot_id` is populated only after exact BotBuild verification. Worker-declared identities, evidence references, records and body digests are checked against BC04, BC15, fixture or gateway authority before import.

Fixture import requires a verified fixture bundle and network-none execution binding. Live import requires gateway evidence scoped to the current tenant, run, attempt, generation, source, policy, release and build. The two evidence sources use separate adapters and cannot be substituted for each other.

## Replay and failure

While an attempt is current, custody and BC05 staging/sealing use stable idempotency identities. Finalization delegates to BC04's reservation/grant protocol. A completed-result replay is read-only: it requires a successful BC04 run with a reservation and publication receipt, recomputes the deterministic OutputSet identity from the verified bundle, and confirms the exact published BC05 binding and manifest. It does not repeat custody or stage operations.

Lost publication acknowledgments continue through the existing BC04 reconciliation path. A failure before reservation does not gain a new durable import scheduler or automatic cleanup guarantee; partially staged sets remain owner-managed and are not visible to published-only reads.

## Contract and resource limits

Payload validation resolves the exact BC04 execution-bound `PayloadContractRef` through the BC08 installed registry. BotRelease v2 binds this reference for live admission; fixture attempts carry it in the immutable BC04 binding. The resolver verifies SHA-256 over exact UTF-8 schema bytes and supports only bounded, self-contained Draft 2020-12 documents. It does not approve contracts, fetch `$ref` targets, or fall back to another revision. Historical BotRelease v1 documents remain immutable and cannot authorize new M3 admissions; unbound historical runs fail closed for new imports. The bundle reader rejects closed-membership, path, symlink, size, strict JSON, digest, identity, and provenance violations. Existing per-file, aggregate result, response, evidence, deadline, and custody upload bounds remain in force. The importer still materializes bounded records and evidence bodies; this increment makes no streaming or performance claim.

Run `make acquisition-application-tests` for focused tests and `make execution-coordination-fixture-acquisition` / `make authorized-live-json-acquisition` for the local integrated profiles. The acceptance composition and expected records remain in `tooling/`; production validation does not import test oracles.
