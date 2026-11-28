# ADR 033 — Scout transcript and current Playbook authoring handoff

**Status: Proposed.** M8 Functional Conformance has implemented and validated this handoff with controlled sources. The ADR does not approve real-source discovery, protected deployment, or a production release workflow.

## Context

BC14 emits evidence-backed `PlaybookProposal` records and controlled HTTP transcripts. Historical proposal v1 is narrative-only. M8 adds an additive proposal v2 carrying a candidate revision, exact declarative candidate, and canonical digest; transcript draft/v4 carries append-only proposal revision history. BC15 owns candidate semantics, and M4 owns immutable Playbook registration and release approval. Discovery evidence remains distinct from live acquisition `FetchEvidence`.

## Decision proposed

The controlled HTTP transcript advances additively to draft/v3 and records each observed page's declared media type. Historical v1/v2 remain readable with their original shapes. BC15 owns a deterministic capability projection and an opt-in adapter that creates a v4 raw-only candidate only from eligible single-page v3 evidence. The candidate binds the declared media type and uses the minimum of the approved response budget and the existing 2,000,000 decimal byte profile limit. JSON authoring text is bounded and redacted; PDF is opaque and remains in BC06 custody. A proposal revision binds the exact candidate through canonical JSON SHA-256. BC14's proposal workflow obtains a currently authorized transcript through its public application, verifies custody references, source/revision, approved discovery scope, findings, and candidate/evidence digests, then delegates semantic validation to BC15.

Proposal validation and promotion are separate operations. The proposal workflow validates the exact immutable revision and candidate digest, checks evidence through BC14's owner interfaces, and delegates acquisition semantics to BC15. The operational reviewer adapter derives identity from the operating-system UID and registers only the exact validated candidate through M4 immutable Playbook registration. It does not create a BotBuild, Release, approval, execution authority, or CaptureRun. The deterministic controlled acceptance now exercises raw PDF, structured JSON, and parameterized JSON from Scout discovery through BC13, BC04, Gateway, trusted import, BC06/BC05 publication, and independent output verification. Parameterized inputs reuse one build/runtime/release. Acquisition `FetchEvidence` remains distinct from discovery evidence.

MCP is an adapter, not a BC14 domain or application dependency. Deterministic proposal, validation, promotion, compilation, and acquisition acceptance do not require an LLM provider, an active Codex MCP session, or a protected deployment. The protected authoring tool surface excludes reviewer and release authority. Actual agent-assisted authoring is explicitly deferred by owner scope decision to a future Protected Agent Operational Acceptance gate; it remains required before using that operational modality against real external sources.

## Alternatives considered

- Mutate the existing transcript v2 shape: rejected because it would silently change an immutable internal contract.
- Treat a narrative proposal as an executable Playbook: rejected because no authoritative declarative plan or promotion boundary exists.
- Add an agent-owned compiler or production spider: rejected because BC15 already owns compilation and the worker/runtime owns execution.
- Allow a local authoring process to bypass the broker NO-GO: rejected because guidance does not enforce egress or filesystem isolation.

## Consequences and limits

The capability surface is projected from schemas and BC15 constraints and is descriptive, not an authorization grant. The local synthetic HTTPS/Squid/DNS fixture and PostgreSQL migrations pass repository checks; TLS verification remains enabled. `codex mcp list` is configuration evidence only, not proof of effective tools in an active agent session. No protected Linux broker, trusted reviewer account, managed MCP allowlist, or legitimate one-use source approval is provisioned. No actual agent-assisted or external-source discovery is claimed. The deterministic journeys establish M8 functional conformance without claiming production RBAC or protected operational isolation.

The raw-only v4 artifact maximum remains 2,000,000 decimal bytes. Optional Linux bundle/systemd and client tunnel/MDM templates are operational examples only and are not required by normal development, `make check`, or deterministic conformance. Sustainable evidence lifecycle and operational deployment readiness remain future work. ADR 033 remains Proposed pending human review.
