# Scout MCP tool host

The current experimental local MCP host composes existing BC14 use cases and exposes closed STDIO tools:

| Tool | Purpose |
| --- | --- |
| `scout_preflight` | Resolve a registered source revision and active local approval; return intersected origins, path prefixes, and limits. |
| `scout_start` | Start a session using host-bound principal and the preflighted source revision. |
| `scout_fetch` | Fetch one HTTPS page through the existing HTTP adapter and Squid egress broker; return bounded extracted text and durable evidence references. |
| `scout_record_finding` | Append an evidence-backed observation or hypothesis. |
| `scout_propose` | Submit a BC14 PlaybookProposal to the durable BC15 handoff/outbox. |
| `scout_status` | Return an authorized, redacted session summary. |
| `scout_export_transcript` | Reauthorize the session, verify referenced custody evidence, and return the versioned metadata-only transcript and digest. |
| `scout_export_replay_fixtures` | Reauthorize the proposed session and write verified captured bodies below the launcher-configured artifact root for offline replay. It returns paths/digests, not body content. |

Tools do not accept caller-supplied identity, policy, approval, budget, credentials, or output path. Source registration and approval are separate administrative actions. The URL alone is not authorization. The host is an adapter and delegates policy, budget, session, evidence, and proposal rules to application/use-case interfaces.

## Operational guarantees and limits

- The current local composition uses a local source/policy file, PostgreSQL, local filesystem custody, and Squid. These are development references/doubles, not productive BC01/BC06/BC11 adapters.
- The implemented fetch path requires the configured Squid proxy; policy and budgets apply to each page and redirect hop. The E2E suite checks that path against a controlled TLS fixture and denies out-of-scope access.
- Exported fixture bytes are read only after current session authorization and custody identity, size, and digest checks. The export destination is a launcher-configured absolute directory outside the checkout; a tool argument cannot choose another path.
- Source response data is untrusted. The local MCP host returns only the bounded text projection for exploration; raw evidence remains in custody and is not placed in the transcript or fixture-export tool result.
- The Codex skill is not an OS sandbox. This setup does not block shell or other tools from network access and does not demonstrate that the Codex process is isolated. Use an operating-system or container boundary when that assurance is required.
- Browser/JavaScript execution, CAPTCHA/authentication bypass, production source catalog/policy/custody services, cryptographically authenticated approval, runtime execution, and bot compilation are not provided.

See the [Scout authoring runbook](../../../../docs/operations/scout-authoring-tools.md), the [HTTP security assessment](../../../../docs/architecture/assessments/scout-http-threat-matrix.md), and the [controlled HTTP E2E assessment](../../../../docs/architecture/assessments/scout-http-exploration.md).
