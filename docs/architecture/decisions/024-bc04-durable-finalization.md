# ADR 024 — BC04 durable execution and BC05 finalization reservation

- Status: Proposed
- Date: 2026-11-25
- Owner: BC04 Execution Coordination; BC05 owns publication
- Scope: durable Run/attempt state and a bounded cross-store authorization protocol

## Context

The fixture-only BC04 coordinator loses state on process exit. A current-attempt check in PostgreSQL followed by BC05 publication in MongoDB is racy: expiration, cancellation or attempt replacement can occur between those operations. PostgreSQL and MongoDB do not share an ACID transaction. BC04 must own operational attempt decisions, while BC05 remains the sole owner of OutputSet membership and publication.

## Decision proposed

1. Keep the existing Python BC04 bounded context and fixture-only worker boundary. Store BC04 Runs, attempt history, lease state, generations, finalization reservations, command receipts and BC04 outbox events in its PostgreSQL schema. Use database time for lease and deadline decisions.
2. Serialize Run transitions with a short PostgreSQL transaction and a locked Run row. Attempts receive monotonically increasing generations; stale lease tokens/generations cannot report failure or reserve finalization. Command operation identity plus canonical intent digest makes retries idempotent and rejects changed intent.
3. Permit one OutputSet per Run in this increment. Finalization reservation atomically binds tenant, Run, current attempt, generation, OutputSet, exact sealed manifest digest and publication operation. The commit is BC04's authorization linearization point. While unresolved, the reservation blocks attempt replacement.
4. BC04 issues a versioned, narrowly scoped Ed25519-signed publication grant. BC04 retains the private signing seed; BC05 receives only a key-ID-indexed public verification key and atomically publishes only the bound sealed OutputSet with its receipt, operation receipt and outbox event. BC05 MongoDB commit is a separate publication linearization point.
5. BC04 marks Run success only after validating the durable BC05 PublicationReceipt against the reservation. A lost response is reconciled by receipt lookup and same-grant retry; it does not trigger a new acquisition attempt.
6. Cancellation before reservation wins and prevents publication. Cancellation after reservation is recorded but cannot revoke the grant. Unresolved reservations do not expire automatically; the safety choice can block liveness and needs operational review/reconciliation.
7. The draft uses Ed25519 with RFC 8785 canonical JSON and a key identifier. Local/test composition may derive the verifier key from its fixture seed, but production BC05 must receive only the public-key ring. This does not establish a production HTTP identity boundary, BC11 authorization, a key management service, distributed ACID, exactly-once delivery or high availability.

## Consequences

- BC04 and BC05 retain independent persistence ownership and private models; only versioned public grant/receipt contracts cross the context boundary.
- MongoDB publication may commit before BC04 terminal completion. Reconciliation converges from the receipt; a published OutputSet is never rolled back because the coordinator crashed.
- A reserved Run cannot be replaced while BC05 is unavailable or the result is ambiguous. This intentional availability trade-off must be monitored and reviewed before production.
- The draft supports one OutputSet per Run. It does not promise atomic publication across multiple sets.
- Durable outbox records establish local event facts only. No broker or delivery guarantee is introduced.

## Validation evidence and remaining decisions

The implementation branch tests independent PostgreSQL clients for attempt acquisition and lease generation, reservation/cancellation ordering, BC05 public-key-only Ed25519 verification, and a synthetic PostgreSQL/MongoDB/RustFS flow with an injected lost acknowledgment after MongoDB publication. A bounded operational reconciliation pass uses the coordinator application use case and BC05 published application API. This local evidence is not production readiness. Review must still decide production service authentication, private-key custody/rotation operations, unresolved-reservation alerting, retention of command receipts/outbox records and future multi-set semantics.

The existing fixture-only networking and worker security boundary is unchanged. This ADR remains Proposed until human approval.
