# Scout MCP tool host readiness — PR #7

Audit snapshot: 2026-11-12. This document records the pre-implementation state for `feat/scout-mcp-tool-host`.

## Baseline and PR #6

- Local `main` and `origin/main` were both at `d9fb5cca54537414a1ed97cb9c7128186ed0bec1` after fetching `origin/main`.
- PR #6 (`feat/scout-tool-host-sandbox` → `main`) is **MERGED**, head `40acfbbfb70747439652d94629e1b5a08b975c12`; its remote `checks` job completed successfully. The implementation branch is created from updated `main`; it does not alter or depend on the PR #6 branch.
- Initial Git state: `main...origin/main`, with only untracked `data-mozaic.zip`. It is user data and is preserved, not included in this change.
- Environment observed: Python 3.12.12 via `uv run`, uv 0.9.8, Docker Compose v2.40.3. `python` is not on PATH; use `uv run python`.

## Existing ownership and implementation

BC14 owns `ScoutSession`, observations, manifests, findings, proposals, PostgreSQL session documents, command receipts, custody intents, and proposal outbox. `ScoutApplication.run()` is the fixture demonstration path; `ScoutRequest` v1 is an existing draft and remains unchanged. The domain has conditional state transitions, fixed source revision/scope, and rejects `independently_verified` findings without an independent-verification use case.

BC01/BC11 are represented by `ScoutApplication` ports and a local development `LocalAuthorizationStore` in the discovery adapter. BC06 is represented by custody ports/fakes; no production custody adapter exists. The experimental HTTP discovery adapter has separate trusted context, authorization, budget, evidence, trace, clock, and transport ports. PostgreSQL recovery/outbox and the Docker/Squid sandbox already exist. BC15 owns transcript-to-draft validation in `collector_engineering`; its schema is illustrative. Do not import BC14 internals into BC15.

## MCP SDK assessment

The official Python SDK repository documents v2 as stable, MIT licensed, Python 3.11+, and supports STDIO. The 2026-07-28 protocol served by v2 removes the `initialize` handshake; the SDK can still serve earlier clients. Since this PR explicitly gates the legacy initialization handshake, the host will use the official `mcp` 1.x maintenance line (`>=1.30,<2`) and the `FastMCP`/`stdio_server` API, with the exact resolution recorded in `uv.lock`. This is a deliberate compatibility tradeoff: 1.x receives security fixes only. Revisit v2 when the required handshake is revised or test legacy-client interoperability with a v2 host. The SDK belongs to the Scout host package only, not BC14 domain.

## Gaps and risk register

| Gap | Pre-change evidence | Required treatment in PR #7 |
|---|---|---|
| Incremental BC14 use cases | Application exposes monolithic `run`; domain already has append/check operations | Add application operations over existing aggregates and repositories; do not change Scout v1 or fixture demo |
| Tool identity | Experimental tools depend on `TrustedToolContext`, but no MCP host supplies it | Launcher-owned context; never accept principal/call/approval/budget in model args |
| Local source and approval config | Existing local JSON policy store validates registered source and approval scopes | Reuse as development-only config, loaded by launcher, require explicit source ID/revision |
| Durable local evidence custody | BC06 is currently a fake in tests | Add a reference-only local file custody adapter with deterministic identity, atomic writes and digest reconciliation, outside the repository |
| Hermetic navigation | HTTP adapter supports injected transport and PR #6 adds worker egress topology | Use fixture transport for this PR; no public network capability through MCP |
| Progressive calls and receipts | Existing receipts are tied to `RunScout` idempotency; session saves have CAS | Keep commands versioned/idempotent via existing repository semantics; avoid schema migration unless implementation proves necessary |
| Transcript export | BC15 validates host approval record and evidence-linked findings, but trusts input JSON structure | Export only from host source of truth after authorization and digest checks; deterministic ordering/redaction; state authentication limitations |
| SDK initialization | SDK v2 protocol has no initialize handshake | Use pinned official SDK v1 maintenance branch to satisfy explicit protocol gate |

## Patch plan and responsibilities

1. `scout.application`: own source resolution, authorization, progressive session lifecycle, evidence/finding/proposal invariants, reauthorized status, and transcript projection.
2. `scout.tool_host`: thin MCP tools with closed schemas and stable typed errors. Model arguments contain only operation data and opaque session IDs.
3. Host composition/adapters: launcher-owned principal and policy configuration; local durable custody; hermetic fixture navigation; existing PostgreSQL repository and outbox; injected clock; redacted observability to stderr.
4. Tests: MCP STDIO client handshake/list/call/error/shutdown; authorization and malicious-argument rejection; source/session/evidence isolation; deterministic transcript and BC15 golden workflow; existing integration profile for durable recovery.
5. Documentation: local-only threat boundary, configuration and transcript proof limits; roadmap and runbook updates. No ADR status changes, public v1 schema changes, historical migration edits, or external navigation.

## Readiness decision

PR #6 supplies an optional egress topology, not an agent sandbox or MCP integration. PR #7 is suitable for a local hermetic STDIO host only if launcher identity and policy remain outside tool arguments, the local custody adapter is explicitly non-production, and no public fetch path is exposed. Process-level agent containment and production BC01/BC06/BC11 integrations remain separate gates.

## Follow-up closure audit (2026-11-13)

The sections above are the preserved readiness snapshot from before closure work and no longer describe the current uncommitted tree. The second audit found the ZIP mentioned by the earlier snapshot absent and untouched. PR #6 is merged; `gh pr view 7` confirms there is no PR #7 and the state branch is still rooted at the current `main` commit. The new authoritative implementation/gate record is [scout-mcp-pr7-closure.md](scout-mcp-pr7-closure.md).

Implemented after that snapshot: `scout_explore_fixture`, local reference custody, incremental command receipts with CAS/replay, transcript `scout-transcript-draft/v1` and cross-reference/digest validation, plus BC15 wheel/sdist resources through `importlib.resources`. The full hermetic MCP→fixture→evidence→proposal→transcript→BC15 CLI draft/validate/replay test now passes. These additions do not change Scout v1 or historical migrations and do not provide public navigation or production BC06/BC11 integration.

At the readiness snapshot, the branch was **NO-GO** for PR creation because the PostgreSQL recovery and Docker sandbox gates had not run. The final pre-publication revalidation subsequently passed those gates with zero PostgreSQL skips. See the closure assessment for exact command evidence and remaining limits.
