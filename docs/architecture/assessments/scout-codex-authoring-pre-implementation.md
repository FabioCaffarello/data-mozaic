# Scout Codex playbook authoring — pre-implementation assessment

Date: 2026-11-19; base: `main` at merge `5c3e2b14f758b1956e6ef9acb5d937b12109f234` (PR #8).

## Repository and baseline

PR #8 is merged into `main`; its merge commit has parent `bbc5df029e2f833437d8ed341f1d036185dc2819`. The initial checkout was clean on `feat/scout-authorized-http-exploration`. `origin/main` was fetched, local `main` fast-forwarded, and the working branch `feat/scout-codex-playbook-authoring` was created from that commit. No pre-existing changes were present.

Before implementation, `make check` passed: 98 tests passed and 25 Docker/PostgreSQL integration tests were skipped because their optional runtime configuration was absent. Lock, lint, formatting, docs, migrations, naming, architecture, contract validation, package installation, and offline replay checks passed. This baseline does not establish the required HTTPS, PostgreSQL, or DNS security gates.

## Existing capabilities inspected

- BC14 owns the session, authorization snapshot, incremental HTTP capture, findings, proposal/outbox, and redacted transcript export. Its local composition resolves registered source/revision and active approval from an administrative file, persists session/budget state in PostgreSQL, routes HTTP through the configured Squid proxy, and stores bytes in a local BC06 custody reference adapter.
- The MCP STDIO adapter exposes `scout_preflight`, `scout_start`, `scout_fetch`, `scout_record_finding`, `scout_propose`, `scout_status`, and `scout_export_transcript`, plus a separate hermetic fixture tool. Its schemas are closed. Identity, principal, policy, budget, and custody paths are launcher-bound.
- The pre-existing Codex skill already describes fail-closed authorization and untrusted source content, but contains stale statements that no MCP host exists and its workflow is incomplete for the current HTTP path.
- BC15 already owns `build_draft(transcript)`, draft/v1 schema and semantic validation, and offline replay from digest-checked captured fixtures. The CLI commands are `draft`, `validate`, and `replay`; no DSL change is indicated.
- The controlled HTTPS Compose profile already exercises MCP → BC14 → Squid → TLS fixture, response custody/digest, secret redaction, an injected instruction in source text, findings/proposal, transcript export, PostgreSQL budget, and restart replay. It did not call preflight, run the BC15 draft/validate/replay pipeline, materialize a review artifact set, or assert that only the intended HTTPS fixture request reached the fixture.
- Operational docs correctly state that the local adapters are doubles and the MCP host does not sandbox Codex or its shell. The skill reference `references/tool-host-requirements.md` is stale and describes the pre-PR7 state.

## Gaps and planned changes

1. Extend the controlled HTTPS deterministic E2E to exercise authorized/denied preflight, controlled link traversal, findings and a BC14 proposal, transcript digest/schema validation, BC15 draft generation/validation, custody-backed fixture replay and a review summary. Assert response custody, redaction, request budget, allowed network trace, and that prompt injection causes no scope change or unauthorized request. Keep the live Codex skill demonstration clearly separate from the Python/MCP deterministic test.
2. Update the local skill and its stale references to the current MCP tools and the ten-step process. Make source registration and approval explicit administrative preflight; make the URL alone insufficient. Document the source content as untrusted and the absence of process-level sandboxing.
3. Provide a reproducible Codex STDIO launcher and a non-secret example environment file. The launcher must fail closed when local policy, database, evidence root, or egress broker configuration is missing. Do not check in authorization grants, credentials, or environment-specific absolute paths.
4. Document the output layout and local operation, with exact CLI commands and artifact provenance. Preserve BC14/BC15 contracts and owners; no business rules move into the MCP adapter and no production bot compiler/runtime is added.
5. Update the roadmap/operational assessment to distinguish platform E2E evidence from a real Codex run, including production-adapter and agent-isolation limits.

## Architectural decisions

Reuse the existing Scout MCP host, BC14 transcript contract/application API, local BC06 custody adapter, authorization test profile, BC15 `build_draft`/CLI, and draft/v1 schema. The integration is at the exported transcript boundary; BC15 remains the producer of the playbook candidate. The ACK/outbox receipt remains receipt of delivery and is not approval. No ADR status change or contract version change is justified by the inspected implementation.

## Acceptance limits at audit time

The local approval file, custody filesystem, and DSN-backed repository are development adapters, not production BC01/BC06/BC11 services. The Codex process and shell retain network access outside the MCP path; a skill is guidance, not a sandbox. No live Codex E2E has yet been demonstrated. The required Docker PostgreSQL/HTTPS/Squid and adversarial DNS checks must run before a GO decision; skipped baseline tests alone are not acceptance evidence.
