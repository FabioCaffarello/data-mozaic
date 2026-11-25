# ADR 026 — BC15 versioned Scrapy runtime framework

- Status: Proposed
- Date: 2026-11-25
- Owners: BC15 Collector Engineering, Scrapy Collector worker
- Scope: lifecycle ownership, generated BotBuild shape, runtime distribution and compatibility for the currently supported Scrapy fixture/live profiles

## Context

The Scrapy fixture and live compiler backends generated most of their own spider, Scrapy process setup, request progression, operator calls, result handling and cleanup. The protected worker already selected and verified immutable BotBuilds, launched a fresh child process and enforced isolation, cancellation and timeout. The authorized acquisition gateway separately owned live operation admission, destination policy and trusted `FetchEvidence`; BC04/05/06 owned execution/finalization, output publication and artifact custody.

M0's comparative assessment recommended a governed Scrapy runtime boundary and a thinner generated entrypoint while retaining exact embedded runtime identity. M1 implements this boundary within the existing BC15 application. It does not establish an independent package or service.

## Proposed decision

1. BC15 continues to own Playbook validation, semantic/capability validation, Typed IR, deterministic lowering, build identity and runtime compatibility metadata.
2. `collector_engineering.scrapy_framework` is the internal BC15 framework API. Its constrained entrypoints are `run_fixture(plan, runtime_identity, argv)` and `run_live(plan, runtime_identity, argv)`. It interprets only compiler-produced closed plans and owns the Scrapy lifecycle, profile settings, supported extraction/pagination operators, bounded result assembly, diagnostics and cleanup.
3. Fixture and live profiles remain distinct. Fixture transport resolves only verified fixture identities and has no network fallback. Live transport delegates requests to the authorized acquisition gateway. The framework cannot authorize a source, choose trusted gateway policy, admit a BC04 attempt, create BC06 custody records, publish BC05 outputs, or finalize execution.
4. A BotBuild embeds the exact first-party runtime file set and records per-file hashes plus the aggregate digest. New builds identify `collector-runtime/1.1.0`. Runtime lookup is never mutable or resolved as `latest`.
5. Generated Python contains an immutable compiled plan, the exact runtime identity and a small entrypoint. It does not contain a free-form plugin interface, infrastructure configuration, credentials, authority signing keys or agent-generated executable behavior.
6. The worker continues to select the exact catalog `(bot_id, build_id)`, verify the build/runtime bundle, launch a fresh child process, apply the protected environment, enforce timeout/cancellation and return untrusted worker results to the trusted importer path.
7. Historical BotBuilds are immutable. Explicit allowlists continue to recognize the prior `collector-runtime/1.0.0` identity and Scrapy runtime compatibility `1.0.0`; new builds bind `1.1.0`. A build is executed only with the runtime bytes embedded in that build and accepted by the worker compatibility policy. No old build is silently recompiled or rebound.
8. The framework remains in the BC15 application until evidence justifies a distribution boundary. Package placement depends on owner/API, runtime identity, compatibility, independent test and dependency boundaries, and actual compiler/worker integration. The library two-consumer extraction heuristic does not prohibit an independently governed framework when those criteria are met.

## Alternatives considered

| Alternative | Decision | Reason |
|---|---|---|
| Keep lifecycle generated in each backend | Rejected | Duplicates lifecycle and cleanup logic, broadens generated source, and makes a shared runtime correction depend on every generator. |
| Move files cosmetically to `frameworks/scrapy/` now | Deferred | No independent distribution, dependency lock, consumer API or release lifecycle has yet been demonstrated. A private BC15 boundary is lower risk and has concrete compiler/worker integration through BotBuild. |
| Preinstall a versioned mutable runtime in the worker image | Rejected for this increment | It would make the selected build depend on external image resolution rather than its exact embedded runtime identity and complicate rollback/replay. |
| Interpret unrestricted agent-authored Python or arbitrary plugins | Rejected | It bypasses declarative capability validation and expands the trusted execution surface. |
| One universal multi-engine framework | Deferred | No concrete second engine consumer exists; browser semantics and network surfaces are different. |

## Ownership and security consequences

Compiler plan and the runtime bundle are trusted build inputs only after BotBuild verification. Runtime interpretation checks that its modules were loaded from the exact embedded build. Runtime parameters are limited to worker paths and authorized attempt context; source scope and gateway authorization remain enforced by trusted components. The fixture image keeps `--network none`. The live worker retains gateway-only routing. No framework code gains credentials or persistence access. Worker completion remains distinct from trusted evidence, `CaptureOutput`, `OutputSet`, `ArtifactRef` and `PublicationReceipt`.

The refactor does not redesign aggregate resource budgets or result streaming. Current request, page, record, response, output and process bounds remain in force; full response/result materialization remains a known limitation for later measured work.

## Verification required

- Verify deterministic generated BotBuild identities and exact embedded runtime membership/hashes.
- Verify legacy 1.0 compatibility and explicit rejection of unsupported identity/profile/capability combinations.
- Run fixture HTML/JSON/pagination under network denial and live JSON/HTML/natural/planned pagination through the gateway.
- Exercise redirects, denial, gateway restart, cancellation, timeout, failure recovery, evidence/custody/publication regression and a healthy execution after failures.
- Preserve independent conformance expectations; test-only oracles remain outside runtime.

This ADR remains Proposed. Implementation evidence is recorded in the 2026-11-25 acquisition maturity assessment; merge or passing checks do not accept this decision.
