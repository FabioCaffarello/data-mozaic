# Scout Codex local authoring — implementation evidence

Historical snapshot of the initial PR #9 implementation. The absence of a real model session and the gaps found here were addressed by [Scout Codex functional remediation — independent acceptance](scout-codex-functional-remediation.md); use that later assessment for current behavior and gates.

Date: 2026-11-19; branch: `feat/scout-codex-playbook-authoring`; base: PR #8 merge `5c3e2b14f758b1956e6ef9acb5d937b12109f234`.

## Delivered path

The local Codex project config starts the existing MCP STDIO host with only the current Scout tools enabled and prompts for tool calls. The local launcher reads an ignored environment file, uses the lockfile in offline mode, and fails closed when configuration is missing. It does not register a source or create an approval.

The new `scout_export_replay_fixtures` MCP adapter delegates to `ScoutApplication.export_replay_fixtures`. BC14 reauthorizes the session, restricts export to proposal-referenced manifests, verifies available custody identity/size/digest, and reads the exact captured bytes. The adapter atomically writes digest-named bodies plus a replay index below the launcher-configured absolute artifact root, rejects tool-supplied paths, and returns metadata only. BC15 remains responsible for generating the draft, validating it, and replaying it.

The controlled HTTPS E2E now covers approved and unregistered preflight, two governed page requests, digest custody, out-of-scope denial, evidence-backed finding and proposal, transcript hash, custody fixture export, draft generation, validation, replay, and a review summary in the ephemeral session artifact directory. The fixture includes hostile instructions as source data; the harness's follow-up remains policy-bounded, and the denied path causes no extra HTTP request.

## Validation evidence

| Gate | Command | Result |
| --- | --- | --- |
| Offline repo/CI checks | `make ci` | Passed; 98 tests passed, 25 optional DB/Docker tests skipped by generic discovery. |
| PostgreSQL migration/recovery | `make db-up`, `make db-migrate`, `make test-integration` | Migrations current; 21 integration tests passed. |
| Authorized HTTPS/MCP and offline authoring | `make scout-http-e2e` | 2 tests passed, zero skips, Squid 7.7; exactly one GET each to `/docs/page` and `/docs/next`, correct fixture peer `172.30.91.10`, zero blocked-sink accepts. BC15 replay covered both captured pages. |
| Adversarial DNS and recovery | `make scout-dns-adversarial-check` | 2 tests passed, zero skips, Squid 7.7; private DNS answer denied before connection; positive controlled fixture request succeeded; zero blocked-sink accepts. |
| Egress topology probe | `make scout-sandbox-check` | Passed; direct TCP/DNS denied and proxy denied private/unregistered destinations. |
| Codex configuration parse | `codex mcp get data_mozaic_scout` | Codex CLI 0.167.2 read the project config and listed all eight tools, STDIO launcher, database env passthrough, and `prompt` approval mode. |
| Patch hygiene | `git diff --check` | Passed. |

The generic `make ci` suite intentionally skips opt-in integration gates. Those gates were run separately above with the local loopback PostgreSQL profile and isolated HTTPS/DNS Compose profiles. The first DNS runner attempt exposed an incorrect fixture-hit expectation in the test harness; after correcting it to be suite-specific, the DNS gate passed twice.

## Codex demonstration boundary

The Codex CLI successfully parsed the MCP configuration, and the official MCP client exercised the real host over STDIO. No live Codex model walkthrough was run. The request supplied no concrete source URL/source revision for a Codex session, and a skill is not process isolation; running an unsandboxed model against a hostile page would not prove the required agent safety boundary. The E2E results above are deterministic platform evidence and are not labeled as a Codex E2E demonstration. The local skill/runbook documents the separate operator-controlled walkthrough after source authorization and OS/network restrictions are in place.

This increment does not provide productive BC01/BC06/BC11 integrations, cryptographically authenticated approvals, process-level Codex sandboxing, browser execution, Playwright, a bot compiler, or a capture runtime. No public contract or DSL version changed; ADRs remain Proposed.
