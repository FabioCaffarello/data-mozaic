# Scout S1.1 — hardening audit and guarantees

Status: implementation experiment with in-process fakes; architecture remains Proposed. Baseline: HEAD `7a731a1` on `main`; the working tree already contained the uncommitted S1 implementation, ADR 006, v1 draft contracts, fixtures, tests, and documentation. No ZIP file is present in the current filesystem scan; no cause for the earlier reported disappearance can be established from this snapshot. Python bytecode is ignored by `.gitignore` and is not source input.

## Audit of implemented behavior

S1 contains a pure Python domain model, an application workflow, typed ports, JSON Schema edge validation, fixture-based offline CLI, and fakes for source lookup, policy, evidence custody, repository, and BC15 handoff. The schema tests validate structure and examples. The flow tests exercise domain invariants and fake behavior. It does not contain HTTP navigation, an agent, a real policy service, durable repository, object store, or inter-service transport. S1's `verified` flag is supplied by the application after calling the custody fake; it is not independent human verification. No v1 schema or fixture is changed by S1.1.

## Gap matrix

| Category | Verifiable in this increment | Limitation |
|---|---|---|
| A. Domain and fakes | State transitions and terminal states; reference integrity; budgets; denied/expired/revoked/unavailable policy; source revision lookup; same-key request replay and fingerprint conflict; local CAS conflict; returned snapshot isolation; custody verification; deduplicated handoff retry. | These are deterministic process-local behaviors, not service guarantees. |
| B. Simulation only | Lost ACK after BC15 accepted; custody failure before/after write; competing Python threads; orphaned in-memory object and retry/reconciliation through deterministic IDs. | Fakes cannot prove delivery, atomicity, or concurrency semantics of real components. Process termination erases the evidence needed to assess recovery. |
| C. Real infrastructure required | Crash/restart recovery, durable transactions/receipts/outbox, multiprocess coordination, egress enforcement, production authorization freshness, custody/retention/access controls, and end-to-end reconciliation. | Requires concrete infrastructure, threat model, operational policy, and integration tests. |

## Command semantics (proposed contract for the S1 application)

| Command | Identity and key scope | Canonical fingerprint | Preconditions and external effects | Result / replay |
|---|---|---|---|---|
| `RunScout` (public request; includes authorize, explore, preserve, propose, handoff, complete) | `request_id` identifies the session; `(authenticated principal, idempotency_key)` is the deduplication scope. | SHA-256 of canonical JSON (`sort_keys`, compact separators) for the complete request plus fixture SHA-256. | Schema-valid request; requester equals authenticated principal; exact BC01 source revision resolves; BC11 decision is allowed, current, unrevoked, same principal/source; effective scope is the intersection. Effects: custody `store`, BC15 `deliver`, then session commit. | Returns committed session as a detached snapshot. Same key/fingerprint resumes an incomplete attempt or returns completed result; key with different fingerprint conflicts. Completed replay rechecks BC01 and BC11 before returning. |
| Internal repository `Create` / `Save` | Session ID; intent additionally keyed by `(principal, idempotency_key)`. | `Create` retains the `RunScout` fingerprint. `Save` compares expected aggregate version. | Create is unique. Save requires stored version and submitted version to equal `expected_version`. No external effect. | Create collision conflicts. Save increments version only on successful locked CAS; stale writer conflicts without mutation. |
| BC06 `StoreEvidence` effect | Stable `artifact-{session_id}` in fake. | SHA-256 of bytes and equality of full reference/content. | Exact session and source identity. External write may succeed while response is lost. | Retry returns same artifact identity in fake; integrity verification is required before manifest/handoff. Real store semantics unresolved. |
| BC15 `DeliverProposal` effect | `proposal_id`, stable for the proposal; receiver deduplicates by ID and rejects changed content. | Equality of proposal value; contract projection is validated before delivery. | Proposal must reference this session's findings and sealed manifests; current authorization and custody verification precede effect. | `acknowledged` means received only. Lost ACK retries same ID; it does not mean approval or BotRelease. |

There is no generic command bus or durable receipt store in S1. `RunScout` intent records are in-memory indexes to a session, not durable command receipts. A successful completed session is the result record in this fake. An in-progress attempt after an uncertain external result is inferred by inspecting idempotent external fake state; this is not a production protocol.

## Future durable recovery protocol (not implemented)

1. In one database transaction, insert a scoped idempotency record containing canonical fingerprint, caller/tenant, command ID, status and result pointer; load/update the aggregate with `version = expected_version`; persist domain events and an outbox row. A duplicate key with a different fingerprint conflicts. A matching completed key returns the stored receipt only after current authorization to read that result is checked.
2. Dispatch outbox effects using stable effect IDs (`artifact_id`, `proposal_id`) and receiver-side idempotency. Model an attempted effect as `pending → confirmed` or `uncertain`; never report success from a timeout or lost ACK. Persist observed responses separately from inferred status.
3. Reconcile `uncertain` effects by querying the owning BC06/BC15 contract or retrying the same effect ID. For an object without committed metadata, verify identity/digest/access policy and attach it transactionally or quarantine/delete under retention policy. For handoff, retrieve receiver receipt by proposal ID before retry.
4. On restart, scan pending/uncertain outbox records and incomplete command receipts; resume only with valid authorization and unchanged source revision. Preserve terminal command receipts for a policy-defined period, then expire keys only after the maximum retry/replay window and audit obligations are met.
5. Validate with real database transaction/CAS tests, receiver deduplication contract tests, process-kill/restart tests at every side-effect boundary, retention/deletion tests, and egress threat tests. This protocol needs a proposed ADR approved by maintainers before implementation; no database, broker, or object store is selected here.

## Validation statement

Thread tests exercise a lock-protected in-memory compare-and-swap only. Mock/fake success is not evidence of atomicity across repository and custody, durable recovery, distributed consistency, or security of egress. Public Scout v1 contracts remain unchanged and draft.
