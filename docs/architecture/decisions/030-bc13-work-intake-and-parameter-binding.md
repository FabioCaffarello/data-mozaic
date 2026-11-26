# ADR 030 — BC13 Work Intake and bounded parameter binding

**Status: Proposed.** Human architecture approval remains outstanding.

## Context

M4 provides an immutable approved release that binds source revision, policy,
BotBuild, runtime and payload contract. BC04 owns durable run admission and
execution. A logical acquisition input and its parameters need a durable owner
that can safely request admission without taking ownership of a CaptureRun.

## Decision proposed

- Keep BC13 as an in-process Python application in `apps/work-intake` for this
  increment. Its current consumers and composition ports are Python; the Go
  gateway continues to own HTTP operation enforcement. There is no demonstrated
  need for a separate Work Intake network service.
- Distinguish Logical Input, immutable Submission intent, Dispatch identity,
  BC04 CaptureRun, and BC04 CaptureAttempt. A new intentional acquisition uses
  a new idempotency key even if its canonical parameter values are unchanged.
- Persist logical Input binding, exact Input Contract reference, canonical
  parameter bytes and digest, selected BotRelease, and stable dispatch operation
  in BC13-owned PostgreSQL state. Same scoped key plus same intent replays the
  original submission; changed intent conflicts.
- Bind the Input Contract reference to BotRelease v3. Keep v1/v2 historical
  documents unchanged. A release without the exact input binding cannot run
  parameterized Work Intake.
- Limit parameter substitution to one declared scalar query key in a compiled
  live Playbook v3 request and one GET response. Validate values against the
  exact immutable Input Contract at submission. The source policy independently
  constrains allowable query values; neither broad policy eligibility nor
  contract validity substitutes for the per-execution binding. Canonical
  parameter bytes and digest are bound to Submission, Dispatch, BC04 admission,
  the current attempt and signed execution capability. The protected worker
  rejects replacement bytes even when they contain another contract-allowed
  value. Construct the URL with query-component encoding; BC04 verifies the
  initial target digest and rejects further operations for this parameterized
  single-response profile. Existing nonparameterized redirect and pagination
  behavior remains available.
- Dispatch through BC04's existing live admission command with a deterministic
  idempotency identity. Resolve BC04 by that identity before age/release checks
  so a committed admission with a lost acknowledgment recovers its original
  Run. BC13 never reads BC04 persistence directly.
- Keep publication and terminal state in BC05 and BC04 respectively. BC13
  stores the admitted Run correlation and resolves terminal state through BC04.
- Compose the reusable operational facade in `work_intake.adapters.composition`.
  It uses BC13's owner-scoped PostgreSQL adapter, the shared immutable contract
  resolver, the M4 lifecycle release resolver, and BC04's public admission,
  outcome and cancellation operations. The controlled acceptance harness calls
  this facade; it does not own lifecycle decisions.
- If cancellation follows an ambiguous dispatch, resolve the BC04 receipt
  before deciding. A receipt is attached to the pending BC13 intent and
  cancellation is delegated to BC04. Without a visible receipt, keep the
  cancellation unresolved and retry after reconciliation.

## Alternatives rejected

- A Go Work Intake service: no independent deployment or stable HTTP contract is
  required by current consumers, and it would add cross-language service
  integration without improving the BC13 ownership boundary.
- A generic parameter/template language: no current source capability requires
  arbitrary substitution or expression evaluation.
- A message broker or generic queue: owner-local PostgreSQL claims and stable
  BC04 operation receipts cover the current dispatch need.
- Duplicating CaptureRun or terminal outcome state in BC13: it would create a
  second authority for fencing, execution and publication results.

## Consequences and limitations

The controlled acceptance compiles one BotBuild once, approves one BotRelease,
and runs three distinct Input parameter values through BC13, BC04, the protected
Scrapy worker, Go gateway FetchEvidence, trusted result import, BC06 custody,
BC05 OutputSet publication and independent result/artifact reads. It also tests
fresh-coordinator admission recovery after a deterministic ACK-loss injection,
post-admission acquisition failure without publication, cancellation and
revocation. The W1–W18 matrix records exact validation levels and limitations.
Historical Playbook, BotBuild, BotRelease and runtime identities remain
versioned; parameterized v3 does not reinterpret v1/v2 artifacts. ADR 030 stays
Proposed until human architecture approval.

No exactly-once cross-context delivery is claimed. BC13's local durable intent
and BC04's idempotent admission receipt provide recoverable at-least-once
dispatch. Release eligibility is checked before new admission; BC04 remains the
final authorization boundary. Cancellation after admission is delegated to
BC04 and cannot undo already committed publication.
