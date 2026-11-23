# ADR 019 — BC15 multi-engine compilation and fixture-only Scrapy backend

- Status: Proposed
- Date: 2026-11-23
- Owners: BC15 Collector Engineering; platform security review for any future worker policy
- Scope: BC15 build/runtime identity, fixture-only Scrapy compilation and cross-backend conformance

## Context

PR #10 established CapturePlaybook draft/v2, `collector-ir/v1`, a Python HTTP Reference compiler, verified BotBuild manifests and offline fixtures. A second compiler backend is needed to prove that one validated IR can produce a bot using a different execution engine. The implementation must preserve the DSL/IR contract, keep semantic ownership in BC15, and not turn validation into production authorization.

The reference runtime was identified by declared version and lockfile, while generated builds depended on runtime source present in the checkout. That makes post-distribution drift difficult to detect and complicates runner isolation. Fixture inspection also mixed filesystem/evidence-byte checks into the v2 semantic validator.

## Decision (Proposed)

1. BC15 owns DSL semantics, the Typed IR, capability profiles, compilation, build verification and the shared extraction semantics. Engine adapters stay internal to `apps/collector-engineering` until an independent second context consumes a stable technical API.
2. Every BotBuild embeds the first-party runtime modules it consumes and records a canonical per-file SHA-256 map plus deterministic aggregate digest. The verifier checks exact file membership and bytes; generated runners verify that imports resolve to those bundled modules. The digest identifies bytes only; it is not a signature, authorization or release approval.
3. Evidence filesystem inspection moves to a cohesive private BC15 module. Structural and semantic rules, diagnostics, evidence binding and IR creation stay in the existing validator and contract.
4. `scrapy-fixture` is a real compiler backend consuming `TypedIR`. It generates deterministic Spider code with operation-specific callbacks, and uses Scrapy's Engine, Scheduler, Downloader, Request/Response, signals/stats and item pipeline. The initial profile supports only the exercised HTTP fixture, bounded links/pagination, HTML/JSON selection, field extraction and record output subset.
5. Scrapy uses an exact-URL fixture download handler for HTTP/HTTPS, with no standard network fallback. Other schemes, retry, redirect, cookies, extensions, arbitrary middlewares, project settings, handler overrides and feed output are disabled. Linux conformance execution requires a non-root, read-only container with `--network none` and resource limits. macOS `sandbox-exec` is reported as a weaker, platform-specific check.
6. Both backends call BC15's existing parser, conversion and provenance rules over verified bytes/documents. Parsel/lxml are not introduced as an alternate semantic parser. Scrapy owns acquisition/navigation mechanics only.
7. Records carry deterministic page/record sequence keys and are staged until the crawl succeeds. A fatal fixture, scope, integrity, budget or extraction error yields failure and no published partial JSONL.
8. A cross-backend suite builds the same canonical IR into two distinct BotBuilds, verifies each, executes against offline fixtures and compares each against independent example expectations, including output order and provenance.
9. Playwright, distributed worker protocols, production egress, BC05 orchestration, BC16 frontier and BotRelease are outside this increment. Agent-supplied source code is never an execution input.

## Alternatives

### Compile in each worker at execution time

Rejected for this increment: it couples worker deployment to compilers and semantic policy, permits build drift between retries, repeats work and weakens review/reuse. A future worker may do technical preparation, but cannot generate semantics or arbitrary code.

### Compile and verify before dispatch

Selected: the future executor receives an immutable verified BotBuild identity and separately authorized run reference. This keeps compile policy and execution policy distinct and enables backend-specific pools.

### Use Scrapy's Parsel/lxml extraction directly

Deferred: it could reduce conversions and may be faster, but parser behavior around malformed HTML, selector edge cases, text normalization, cardinality and types can differ from the reference parser. First prove semantic equivalence with an explicit conformance suite before changing parser ownership.

### Generic engine plugin framework

Rejected: two named first-party backends do not yet justify a plugin lifecycle, external extension contract, dynamic loading or compatibility burden. Capability profiles and compiler entry points are sufficient.

### Shared runtime library now

Rejected: both compiler backends and the runtime belong to BC15, so a second engine inside the same context is not an independent library consumer. Keep modules private; revisit only when a distinct BC/worker consumes a stable contract or extraction is technically necessary.

## Consequences and trade-offs

- BotBuilds are larger by a small set of first-party modules. Heavy third-party dependencies stay in the locked execution environment rather than being copied into each build.
- Runtime identity and exact membership detect accidental drift but do not establish publisher authenticity; signing/approval belongs to a later release/trust design.
- Scrapy's scheduler and dupefilter are reused; deterministic output needs bounded in-memory ordering of emitted records. The declared record/output budgets bound that buffer.
- Exact fixtures make the first backend offline and testable but do not demonstrate real network authorization, egress controls or production source policy.
- Playwright has a different request surface (subrequests, redirects, service workers and rendered DOM) and must not claim parity with static HTML.

## Verification

- Compile identical fixture examples from the same `collector-ir/v1` to Python Reference and `scrapy-fixture`.
- Verify BotBuild file sets, per-file runtime hashes, aggregate digest, toolchain and dependency lock before execution; test changed, missing, additional and incompatible runtime files.
- Run cross-backend tests in Linux non-root containers with `--network none`, read-only root and resource caps. Probe socket and scheme denial; verify no partial output after failure.
- Compare independently authored JSONL oracles, including types, cardinality, ordering, requested/effective URL, evidence reference, raw SHA-256, operation/page/record identity.
- Keep v1/v2 schemas and the canonical IR golden unchanged. ADR remains Proposed until human approval.
