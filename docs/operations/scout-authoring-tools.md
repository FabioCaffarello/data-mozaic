# Scout authoring tools — local development

**State: NO-GO; protected broker deployment unavailable.** The prior workspace-launched MCP STDIO path is disabled: `tooling/codex-scout-mcp.sh` exits without importing the application. `.codex/config.toml` requires a Streamable HTTP service at `http://127.0.0.1:8765/mcp`, but that service is not implemented or provisioned yet, so Codex startup must fail closed. The existing STDIO fixture server is test-only. The local source/policy file and custody filesystem are development doubles, not production BC01/BC06/BC11 integrations. See [trusted broker deployment prerequisites](scout-trusted-broker-deployment.md) and Proposed [ADR 017](../architecture/decisions/017-scout-trusted-mcp-deployment.md).

## Local setup

The protected MCP workflow is not available until the separately installed HTTP broker and the managed MCP identity requirement are both verified. Do not start the prior workspace STDIO host. The existing `make scout-dev-*` commands support deterministic local fixtures and status diagnostics; they do not provision the trusted broker or authorize a source. Use `make check` and the protected replay E2E for local validation, not a live Codex fixture session.

## Preflight e decisão de autorização

An input URL never creates a source or grants access. An administrator must perform these steps before the agent workflow:

1. Confirm ownership or written authorization for the exact host and collection paths.
2. Add the hostname to the broker allowlist in `.local/scout-egress-domains.txt` only when needed; keep private, loopback, link-local, and reserved destination denies enabled.
3. Register a specific `source_id` and revision in `.local/authoring-policy.json`, with HTTPS origin, narrow path prefixes, GET only, finite request/time/response-byte limits.
4. Add an active approval for the exact local principal and same source/revision with equal or narrower scope and limits, and a short explicit expiry. Do not add credentials, cookies, authorization headers, or secrets.
5. Start the configured host and call `scout_preflight(source_id, revision)`. Compare its returned origin/path/limits with the requested URL and task. The Codex workflow must stop if the lookup or approval is absent, expired, broader than intended, or mismatched. Chat approval, skill instructions, and URL ownership claims do not update the local policy.

`config/scout/authoring-policy.example.json` is intentionally empty. No valid source/approval is provisioned by this repository. The JSON policy format is validated by the local loader; the concrete policy change is a separate operator action outside MCP.

For a worktree-local synthetic acceptance, `make scout-dev-setup` runs the locked workspace sync, prepares owner-only local templates, starts the loopback PostgreSQL profile, applies migrations, creates a private short-lived HTTPS fixture CA, and starts the controlled Squid/DNS/HTTPS Compose profile. The host-side Squid proxy is fixed at `127.0.0.1:3128`, matching the Scout transport's enforced proxy endpoint; setup fails if that port is occupied. The generated MCP artifact root is a worktree-specific, owner-only directory under the operating system temporary directory, so BC15 can reject unsafe mount ancestors without weakening its filesystem checks. Temporary-directory cleanup may remove these private review artifacts. Setup does not authorize any source. Run `make scout-dev-status` to see preflight diagnostics, including worktree, migrations, fixture stack, source approval, launcher, BC15 wrapper, and Codex login. The only bundled authorization action is `make scout-dev-authorize-single-fixture`; it explicitly registers and approves `e2e-catalog-single@single-resource-v1` for `/api` on `catalog.fixture.test`, GET only, one request, 120 seconds, and 4 KiB, expiring after 30 minutes and consumable by one Scout session. It preserves unrelated policy entries and refuses broader conflicting registration. Do not use this test-only action for other hosts or sources.

After status reports ready, `make scout-authoring-acceptance` starts a real `codex exec` session from the current worktree, grants its workspace-write sandbox access only to the private artifact root, and directs it to load this skill. Raw event logs and artifacts stay outside Git with owner-only modes; a second event log redacts tool arguments and command output. The Codex process has no global network isolation: Scout's Squid policy governs only requests sent through the MCP host. The deterministic Make targets remain separate from this real-model acceptance.

## Intended MCP tool contract (unavailable until trusted deployment)

`scout_preflight`, `scout_start`, `scout_fetch`, `scout_record_finding`, `scout_propose`, `scout_status`, `scout_export_transcript`, `scout_bc15_replay`, `scout_bc15_accept`, `scout_bc15_status`, `scout_bc15_get_manifest`, and `scout_bc15_get_report` are the current authoring tools. `scout_explore_fixture` is a separate hermetic test/demo tool and is not the HTTP source workflow. Schemas reject extra arguments; principal, policy, approval, budgets, evidence root, Docker runtime, image, volumes, and credentials are launcher-owned.

- `scout_fetch` accepts one HTTPS page, resolves source/revision from its session, reauthorizes each redirect hop, spends the durable PostgreSQL request budget, stores final response bytes in local custody, and returns bounded extracted text, links, and evidence IDs/digests. Links are suggestions; none is followed automatically.
- `scout_export_transcript` reauthorizes the session, checks proposal/finding/evidence references, verifies custody, and returns the versioned BC14 transcript plus its SHA-256. HTTP exploration exports internal transcript draft/v2 with requested/effective start URLs and ordered observations; the hermetic fixture workflow may still export v1. The JSON transcript intentionally contains metadata and references, not raw body bytes.
- `scout_bc15_replay` and `scout_bc15_accept` reauthorize the session, obtain proposal-scoped bytes from BC14/BC06 in memory, and pass a bounded request to a pinned BC15 executor image. The model cannot send fixture/output paths, expectation paths, Docker options, commands, mounts, URLs, or runtime identity. Acceptance selects an operator-configured expectation by the authorized source/revision.
- The executor receives no network, Docker socket, host bind mount, capabilities, writable root filesystem, or inherited credentials. It writes only to a new named volume with CPU, memory, process, input-size, concurrency, and wall-time limits. The Codex `workspace-write` process is denied access to the daemon socket; this was confirmed with a connect-only test on macOS. Docker Desktop stores the named volume in its Linux VM. Linux CI uses the runner kernel and does not claim VM-level or kernel-escape protection. If Docker/runtime verification or image preparation fails, the protected tools are not exposed.
- `scout_bc15_status`, `scout_bc15_get_manifest`, and `scout_bc15_get_report` require the same authorized session and run reference; they return bounded metadata, never raw bodies. The raw result remains in daemon-managed storage and is not copied to the agent workspace.
- Protected run IDs, volume ownership labels, identity/session binding, state, expiry and bounded reports persist in a private local SQLite adapter registry; see [BC15 run retention and recovery](capture-playbook-drafts.md#protected-run-lifecycle). No MCP operation accepts a Docker volume identifier or requests cleanup.
- At startup the broker snapshots the owner-only policy file into a mode-`0400` file under its private state root, then uses that snapshot for fetch and redirect authorization. This keeps worktree policy edits from changing the already-running broker's scope; the operating-system sandbox must still deny the agent writes to the private state root.
- `scout_record_finding` supports `observation` and `hypothesis`; there is no independent verification tool in the current contract.
- `scout_propose` submits a BC14 `PlaybookProposal` to the durable BC15 outbox. Delivery acknowledgement means received, not approved.

The current composition supports one origin per source because of the existing `ScoutScope` contract; a multi-origin source fails closed. MCP SDK resolution remains pinned in `uv.lock` at 1.30.0 under `mcp>=1.30,<2` as recorded by Proposed ADR 015.

## Artifacts and offline BC15 pipeline

The artifact directory is `<SCOUT_ARTIFACT_ROOT>/<session_id>/` and contains:

```text
session-metadata.json
evidence-manifest.json
findings.json
playbook-proposal.json
transcript.json
transcript.sha256
capture-playbook.yaml
validation-report.json
replay-report.json
review-summary.md
```

BC15 raw output is stored in a private Docker named volume, not in this agent-writable review directory. Do not treat direct local replay as evidence for the protected mode.

Save the exact `transcript_json` tool result to `transcript.json` and compare its bytes to the returned SHA-256. Use the versioned BC15 wrapper; it resolves the workspace from its own location and accepts absolute artifacts from any current directory:

```sh
tooling/codex-scout-bc15.sh draft /absolute/session/transcript.json --output /absolute/session/capture-playbook.yaml
tooling/codex-scout-bc15.sh validate /absolute/session/capture-playbook.yaml
```

Submit the generated YAML to `scout_bc15_replay`, then `scout_bc15_accept`. Use the returned run references with the protected status/manifest/report tools. The direct Python replay/accept functions remain for trusted in-process tests only; the CLI and Codex wrapper expose draft/validate but no direct replay command.

The internal transcript draft v2 preserves requested/effective start URLs, sequence and observed links. BC15 refuses transcript v1 for generation because it cannot identify the start unambiguously. Structural/semantic validation, replay execution and acceptance are distinct; acceptance uses independent operator expectations and checks required pages, edges, digests, raw output and limits. `output.raw` preserves raw response bytes and does not extract records. No selectors, query pagination, credentials, arbitrary code, browser actions, or productive bot compilation are supported. Missing fixture, digest mismatch, unsupported response, or budget failure must appear in replay; never repair it by fetching again. The CapturePlaybook draft/v1 schema and ADR 013 remain illustrative/Proposed, not runtime or release contracts.

## Validation and evidence boundaries

Run the controlled HTTPS and policy path through `make scout-http-e2e`; it requires a reviewed `.env`, Docker, local PostgreSQL, Squid 7.7, controlled DNS, and a transient TLS CA. Run the independent adversarial DNS gate with `make scout-dns-adversarial-check`; run optional sandbox topology checks with `make scout-sandbox-check`. The integration tests must not be reported as passing if skipped. See [egress sandbox operations](scout-egress-sandbox.md).

The controlled fixture includes a prompt-injection string and a fake credential marker. The host returns only bounded extracted text with the fake credential redacted, retains original bytes under custody, denies an out-of-scope path, and exports metadata-only transcript. Treat the injection as untrusted data; it cannot grant scope or authorize a request. Browser-rendered JavaScript, CAPTCHA/authentication bypass, raw redirect-body custody, production BC01/BC06/BC11, and cryptographically authenticated approval remain unsupported.

The deterministic Python/MCP test proves the platform pipeline against a controlled fixture. It does not prove Codex used the skill or interpret the source safely. A live Codex walkthrough is a separate manual demonstration and must identify its explicitly configured source/revision, active approval, limits, output location, and operator. A passing platform test must never be presented as a Codex E2E run.
