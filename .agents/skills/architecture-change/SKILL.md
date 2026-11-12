---
name: architecture-change
description: Use when changing Data Mozaic context ownership, module dependencies, public contracts, or architecture decisions.
---

# Architecture changes

1. Read `docs/architecture/README.md`, `docs/architecture/assessments/repository-baseline.md`, and the relevant ADRs before editing a boundary.
2. Identify the context owner, consumer, published contract, compatibility impact, and current implementation. A bounded context is not a deployment unit.
3. Keep domain code independent of adapters and vendors. Do not expose aggregates as public contracts or access another context's tables/internals.
4. Keep new abstractions tied to a real or explicitly planned consumer. Prefer composing mature third-party tools over adding generic libraries.
5. For a consequential decision, update a Proposed ADR with alternatives, trade-offs, consequences, risks, and validation criteria. Never change ADR status to Accepted without human approval.
6. Preserve wire contracts unless the task explicitly includes a reviewed versioning change. Mark examples and schemas draft when they are not approved.
7. Run the repository checks appropriate to the change, and report outcomes and remaining limits.
