# Scout MCP tool host

**Deployment status (2026-11-20): NO-GO.** The workspace STDIO launcher is disabled. No trusted Streamable HTTP service or enforced Codex MCP allowlist is installed. Do not treat the historical local composition described below as a trusted broker; it imported workspace code and had host Docker access. The constraints below describe the intended tool contract and deterministic tests, not a currently available protected service. See the [trusted broker deployment runbook](../../../../docs/operations/scout-trusted-broker-deployment.md).

The previous experimental local MCP host composed existing BC14 use cases and exposed closed STDIO tools:

| Tool | Purpose |
| --- | --- |
| `scout_preflight` | Resolve a registered source revision and active local approval; return intersected origins, path prefixes, and limits. |
| `scout_start` | Start a session using host-bound principal and the preflighted source revision. |
| `scout_fetch` | Fetch one HTTPS page through the existing HTTP adapter and Squid egress broker; return bounded extracted text and durable evidence references. |
| `scout_record_finding` | Append an evidence-backed observation or hypothesis. |
| `scout_propose` | Submit a BC14 PlaybookProposal to the durable BC15 handoff/outbox. |
| `scout_status` | Return an authorized, redacted session summary. |
| `scout_export_transcript` | Reauthorize the session, verify referenced custody evidence, and return the versioned metadata-only transcript and digest. |
| `scout_bc15_replay` | Reauthorize the session, obtain verified custody bytes through BC14, and run the submitted draft in the protected BC15 executor. The model supplies neither fixture nor output paths. |
| `scout_bc15_accept` | Run protected replay and an independent expectation selected by trusted source/revision configuration. |
| `scout_bc15_status`, `scout_bc15_get_manifest`, `scout_bc15_get_report` | Return bounded status, manifest and report metadata for a run owned by the authorized session; never return raw body bytes. |

Tools do not accept caller-supplied identity, policy, approval, budget, credentials, or output path. Source registration and approval are separate administrative actions. The URL alone is not authorization. The host is an adapter and delegates policy, budget, session, evidence, and proposal rules to application/use-case interfaces.

## Operational guarantees and limits

- The current local composition uses a local source/policy file, PostgreSQL, local filesystem custody, and Squid. These are development references/doubles, not productive BC01/BC06/BC11 adapters.
- The implemented fetch path requires the configured Squid proxy; policy and budgets apply to each page and redirect hop. The E2E suite checks that path against a controlled TLS fixture and denies out-of-scope access.
- Protected replay reads fixture bytes only after current session authorization and custody identity, size, and digest checks. The broker passes bounded bytes to a fixed BC15 container through stdin; it does not expose fixture files to the model or mount host paths into the executor.
- BC15 raw output is published to a per-run named volume owned by the local Docker daemon. The executor has no network, Docker socket, host bind mount, capabilities, writable root filesystem, or caller-selected output path. The Codex shell cannot connect to the daemon socket in the tested macOS `workspace-write` sandbox. Linux CI shares the runner kernel and does not claim VM isolation or protection from kernel compromise.
- Source response data is untrusted. The local MCP host returns only the bounded text projection for exploration; raw evidence remains in custody and is not placed in the transcript or fixture-export tool result.
- The Codex skill is not an OS sandbox. The workspace-write shell is still able to modify its workspace; it is specifically denied the local Docker daemon socket. The trusted MCP tool is the only executor broker and exposes no generic Docker operation. This setup does not block shell or other tools from network access.
- Browser/JavaScript execution, CAPTCHA/authentication bypass, production source catalog/policy/custody services, cryptographically authenticated approval, runtime execution, and bot compilation are not provided.

See the [Scout authoring runbook](../../../../docs/operations/scout-authoring-tools.md), the [HTTP security assessment](../../../../docs/architecture/assessments/scout-http-threat-matrix.md), and the [controlled HTTP E2E assessment](../../../../docs/architecture/assessments/scout-http-exploration.md).
