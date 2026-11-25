# ADR 022 — BC05 Acquisition Output foundation

- Status: Proposed
- Date: 2026-11-24
- Owner: BC05 Acquisition
- Scope: datastore-independent output domain, draft contracts and local staging/publication semantics

## Context

The repository has fixture-only collectors and BC04 run coordination, but no stable engine-independent acquisition output contract or OutputSet lifecycle. JSONL files and temporary raw references are local worker protocol details, not a durable output domain. The reviewed architecture assigns physical artifact identity/custody to BC06, attempt/run terminal decisions to BC04 and dataset publication to BC09. PostgreSQL remains the control/governance store; MongoDB is the candidate first BC05 output store; MinIO is the intended object store. Those adapters are outside this ADR's first code increment.

Output persistence must support structured records, artifact references and their composite without allowing source-authored database schemas. Repeated observations must remain distinct from idempotent command retries. A set must never be visible as complete while its membership is still changing.

## Decision proposed

1. Keep `CaptureOutput` and `OutputSet` in BC05 Acquisition; do not create BC17. OutputSet is the aggregate root for bounded membership and lifecycle; individual output payloads are separate bounded members and are not embedded as an unbounded aggregate collection.
2. Define a fixed versioned envelope with tenant/source revision, optional run+attempt binding, extensible producer identity, optional capability-specific release/build information, observation/output identity, payload-contract reference and output variant. Bot identity is not mandatory for non-bot acquisition capabilities.
3. Support closed variants `record`, `artifact` and `composite`. Record/composite payload is bounded JSON validated against a versioned contract. Artifact refs identify BC06 logical artifacts and integrity metadata; BC05 does not own bytes, physical locations, access or retention. Artifact-bearing sets require explicit custody confirmation through a BC05-consumed port before publication.
4. Use RFC 8785 JSON Canonicalization Scheme for cross-process intent and manifest digest bytes, constrained to its interoperable I-JSON subset. Pin implementation behavior with RFC examples and repository-owned vectors. Large integers or higher-precision decimal values must be represented by contract-defined strings; Unicode strings are preserved as received without normalization; object field ordering is canonicalized; `null` is distinct from absence; duplicate JSON names, lone surrogates, unsupported values and non-finite numbers are rejected.
5. Use OutputSet lifecycle `STAGING → SEALED → PUBLISHED`, with terminal alternatives `ABORTED` and `REJECTED`. A separate `VALIDATING` state is not required for synchronous local validation. Seal freezes membership and manifest identity. Publish validates the exact sealed digest and is idempotent; a published set is immutable.
6. Keep domain/application independent of database, object storage, worker and transport. Ports are owned by BC05 consumers. An in-memory adapter is a bounded reference implementation, not evidence of durability or distributed fencing.
7. Keep MongoDB as the preferred persistence candidate behind BC05 ports. This ADR does not approve collection/index layout, operational topology, MongoDB adapter, or migration of PostgreSQL-owned contexts.

## Alternatives considered

### Put output lifecycle in BC04

Rejected: BC04 owns coordination and terminal Run state. Acquisition output can be produced by capabilities without a Run and is consumed independently by parsing/quality/pipelines. Coupling payload storage to BC04 would conflate two lifecycles.

### Create a separate Acquisition Outputs context (BC17)

Rejected at present: there is no distinct team, language, lifecycle or operational consumer requiring a new context. BC05 is the producer and owner of capture semantics. Reconsider only with demonstrated independent domain behavior, not because MongoDB is selected.

### Let workers persist source-defined collections

Rejected: it leaks infrastructure credentials and persistence schema into generated code, weakens authorization and creates per-bot schema/index churn. Workers emit the stable BC05 envelope through a constrained sink.

### Use ad hoc Python JSON serialization for digesting

Rejected: process/language serialization differences undermine stable intent identity. RFC 8785 is an existing cross-language specification; application still needs pinned implementation versions, vectors and strict I-JSON validation.

### Keep one mutable set document with all outputs embedded

Rejected: output membership and payloads can grow without bound. Keep member documents bounded and separate; OutputSet manifest stores bounded counts/digests and becomes visible only after seal/publication.

## Local guarantees and unresolved integration

Review amendment, 2026-11-25: the dedicated `feat/execution-coordination-durable` branch implements a draft reservation/grant protocol and adds a BC05 verifier requirement for Run-bound OutputSets. BC04 reservation in PostgreSQL is the authorization point; BC05 MongoDB publication is separate; the stores do not share a transaction. The initial policy keeps unresolved reservations authoritative indefinitely, prevents attempt replacement, lets cancellation win only before reservation, and requires BC04 to complete from the exact durable BC05 receipt. See [execution consistency protocol](../execution.md). This implementation is local/test scoped and does not change this ADR's Proposed status or establish production service authentication, revocation, HA, or exactly-once delivery.

## Consequences and risks

- BC05 public JSON Schemas begin draft; private dataclasses are not serialized aggregates or future cross-language DTOs.
- RFC 8785’s numeric constraints require source contracts to use strings for identifiers/decimals that exceed interoperable binary64 precision; validators must reject silently lossy inputs.
- MongoDB remains reversible because it is isolated behind BC05-owned repository/publication ports; the domain is not BSON-shaped.
- An in-memory adapter cannot provide process durability, uniqueness across processes, transactional outbox, reservation coordination or recovery.
- Artifact publication remains blocked until a custody port confirms required references; structural reference validity alone is not custody proof.

## Validation criteria

- Independently derived valid/invalid contract fixtures for each output variant, manifest and justified receipts.
- RFC 8785 canonical bytes/digest vectors, including field ordering, Unicode preservation, null, numeric constraints, unsupported values and error cases.
- Tests for identity/idempotency conflict, tenant/source/run/attempt binding, stage/seal concurrency, frozen membership, manifest verification, custody gate, abort/reject, publication retry and immutability.
- Root architecture/docs/naming checks and package/root test suites pass.
- Persistence adapters later demonstrate transactional seal/publication/outbox in their owner datastore; integration with BC04 is separately tested with crash injection and the approved durable finalization protocol.

No merge, implementation, or green check automatically changes this ADR from Proposed.
