# ADR 025 — Authorized live acquisition profile

- Status: Proposed
- Date: 2026-11-25
- Owners: BC01 Source Catalog, BC11 Policy and Access, BC15 Collector Engineering, BC04 Execution Coordination
- Scope: draft owner contracts and the boundary required before a live Scrapy worker is admitted

## Context

The existing Python Reference and Scrapy backends compile to fixture-only BotBuilds. The worker catalog accepts only `scrapy-fixture-v1`; BC04's protected Docker adapter uses `--network none`; fixture HTTP/HTTPS uses exact-identity handlers with no network fallback. The source and policy fields in a CapturePlaybook are descriptive inputs, not authorization. A build digest establishes byte identity, not release approval.

Enabling an environment proxy on the existing fixture worker would not be a safe live profile. It would change the reviewed network boundary, allow a worker to attempt direct sockets if its network were attached, and would not make an HTTPS CONNECT proxy enforce encrypted methods, paths, or bodies.

## Proposed direction

1. Keep `scrapy-fixture` and its profile, catalog checks, Docker `network none`, tests, and contracts unchanged.
2. Define separately versioned draft contracts for immutable BC01 `SourceRevision`, BC11 `AcquisitionPolicy`, and BC15 `BotRelease`. An approved release binds a verified BotBuild digest, source revision, policy version, backend/runtime compatibility, runtime image digest, and approval evidence.
3. Admission is owner-composed: source and policy owners establish current eligibility; BC15 verifies build/release compatibility; BC04 durably records the admitted binding. A playbook cannot widen source or policy scope. Revocation blocks new admission but cannot undo requests already sent.
4. Any live worker runs as a separate profile and image. Prefer a private per-attempt HTTP acquisition gateway with a narrow authenticated or filesystem-scoped channel. The worker network must have no direct route; only the gateway can reach the controlled egress network. The gateway resolves and validates every destination, revalidates every redirect, verifies TLS, enforces methods/origins/path scopes/request and byte budgets, and streams bounded response entities to the result channel. Controls belong to the component that can observe them.
5. For controlled local tests, allow an explicitly test-scoped policy bound to a local fixture server. Never place this exception into ordinary external-source policy or the fixture profile.
6. Preserve the existing IR operators for GET, JSON/HTML extraction, finite pagination, and raw output where semantics match. Select a live capability profile at trusted release/admission time, not from the playbook. Live response bodies are decoded HTTP entity bytes unless a separate representation is proven; they are not wire bytes.

7. Implement the acquisition gateway as a separate Go deployable at `apps/acquisition-gateway/`. Its API accepts a bounded GET request and delegates the external HTTP request itself; it is not CONNECT or a general forward proxy. Go is selected for this independently operated network-enforcement boundary, with the existing native Go toolchain and service conventions. No performance advantage is claimed or measured.
8. Do not reuse Scout's `AiohttpSquidTransport` as the gateway. It is a Python GET client that sends traffic through a separately isolated Squid proxy, returns a fully buffered bounded response, disables compression and automatic redirects, and leaves authorization/budget ownership in the Scout tool host and PostgreSQL ledger. It does not provide the acquisition gateway's policy lookup, pinned destination dialing, evidence spool, or worker-facing capability contract. Reuse of the strict URL normalization rules may be considered only after contract-level tests prove identical semantics; no shared library is introduced now.
9. BC04 consumes an operator-managed immutable authority snapshot verified with Ed25519 against a public-key ring supplied by trusted deployment configuration. The snapshot contains source revisions, policies and releases; approval flags outside that signature are claims only. Operators rotate by publishing a higher generation and advancing a trusted generation floor. Revocation prevents new admission after the current snapshot is installed; it does not undo a request already sent. A gateway restart invalidates outstanding capabilities and requires attempt recovery; it must not restore an active in-memory quota.
10. BC04 issues a short-lived Ed25519 `ExecutionCapability` to the gateway audience, scoped to one tenant/run/attempt/generation, approved release, source revision, policy digest and deadline. BC04 retains private signing material; the gateway receives verification keys only. Capability expiry rejects new gateway requests, while an admitted request is bounded by the same execution deadline. The existing finalization reservation remains durable and non-revocable until resolved; it is a separate protocol.

## Alternatives

- Toggle networking on the current fixture worker: rejected because it removes `network none` from the existing security boundary and creates fallback risk.
- Trust proxy environment variables: rejected because code can bypass the proxy and environment is not a network control.
- Reuse Scout Squid unchanged: rejected for acquisition enforcement. Current ACL is authoring-specific, allows only CONNECT/443, and cannot inspect encrypted HTTP paths or methods. It may inform DNS/address denial testing but is not an authorization service for BC15.
- Let generated bots persist outputs directly: rejected; worker output is untrusted and all state continues through the trusted importer, BC06, BC05, and BC04.
- Implement a general policy engine, release registry, or distributed fleet now: rejected absent independent consumers and operational needs.
- A conventional CONNECT proxy: rejected because it cannot inspect encrypted methods, paths, query parameters, or response bodies without TLS interception. General TLS interception is outside scope.

## Consequences and risks

The contracts in this increment remain drafts and this ADR remains Proposed. The branch now implements a bounded local authority snapshot verifier, durable BC04 live admission and attempt-bound capability issuance, the Go delegated HTTP gateway, and a separate JSON live Scrapy compiler/runtime. PostgreSQL stores signed authority envelopes and monotonic heads; signature verification precedes installation, rollback and same-generation conflicts fail, exact replay is idempotent, and admission checks current generation and expiry. Public-key rotation remains an operator-managed trust configuration concern. BC04 exposes a narrow authenticated operation-admission port; the gateway calls it for each outbound hop before dialing, so BC04's run lock serializes new operation admission with cancellation. An admitted request can continue after cancellation and cannot be undone.

The controlled acceptance command has completed a durable authorized run against the isolated TLS source using one real compiled BotBuild and image. It exercised BC04 admission and capability issuance, the protected Scrapy container, the Go gateway's BC04 operation client, trusted gateway evidence lookup, BC06 artifact custody, BC05 output sealing/publication, BC04 receipt completion, importer replay, independent output reads, and artifact digest verification. The fixture profile remains on Docker `network none`; the live worker was tested for direct-source, persistence-host and unauthorized-origin denial. This proves the local controlled vertical only. The command creates test-scoped source, policy and release records and a temporary signing key; it is not a production approval workflow, a BC01/BC11 service, or an authorization for external sources.

The current live compiler and trusted controlled importer support the bounded JSON vertical. HTML extraction and finite live pagination remain unimplemented; the corresponding IR operators remain available to fixture builds. Gateway quotas are durable across process restart, and restart invalidates capabilities issued before that process epoch; an active attempt must recover and receive a fresh capability without resetting its persisted request/byte ledger. The gateway spools decoded HTTP entity bytes, not wire bytes. Evidence and custody objects are retained for governed recovery; this increment does not provide an automated retention cleaner. External source acquisition remains separately authorized.

## Validation criteria

- Fixture build IDs, schemas, security tests, and effective `--network none` behavior remain unchanged.
- Live release cannot execute from fixture-only images or from an unapproved/revoked/mismatched release.
- Every requested URL and redirect is constrained by an eligible source revision and policy; DNS and address checks reject private, loopback, link-local, multicast, and metadata destinations.
- Worker direct egress is denied by actual container/network behavior, not proxy environment variables.
- Controlled HTTP server positive and negative cases run through the exact live profile and trusted import path.
- Result bundles carry bounded decoded response entity bytes, digest, sanitized metadata, and trusted execution identity; malformed and stale results fail closed.
- No real-source execution occurs until a narrowly scoped exploration is approved separately.
- Conformance tests demonstrate that the gateway pins the validated DNS address for the actual socket dial, retains the URL hostname for TLS verification, rejects prohibited IP ranges, and revalidates each redirect and retry against the same trusted policy and shared attempt budget.
