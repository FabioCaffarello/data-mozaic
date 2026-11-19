# Scout authoring tools — local development

**State: experimental local MCP STDIO host.** The Codex workflow composes the registered source/policy lookup, BC14 session/fetch/finding/proposal/transcript use cases, local BC06 custody reference, Squid egress proxy, and BC15 draft/validate/replay CLI. The host is an adapter; domain rules remain in BC14 and formal draft ownership remains in BC15. The local source/policy file and custody filesystem are development doubles, not production BC01/BC06/BC11 integrations. The Codex process and shell are not sandboxed by the MCP host or skill.

## Local setup

1. Start the local PostgreSQL profile using the reviewed `.env` and `make db-up`; apply migrations with `make db-migrate`.
2. Start Squid with an administrative host allowlist in `.local/scout-egress-domains.txt`; publish it only at `127.0.0.1:3128`. The Scout transport requires this proxy and fails closed if it is unavailable.
3. Copy the empty source template and configure a source/revision plus separate active approval as described below:

   ```sh
   mkdir -p .local
   cp config/scout/authoring-policy.example.json .local/authoring-policy.json
   ```

4. Copy `config/scout/authoring-mcp.env.example` to `.local/scout-mcp.env`; set the local principal, policy path, absolute custody and artifact roots outside the checkout, and loopback proxy. Restrict it to its owner (`chmod 600 .local/scout-mcp.env`). Do not put the database URL or credentials in this file or in Git. Keep `SCOUT_DATABASE_URL` in the existing ignored `.env`.
5. Install locked workspace dependencies with `make sync`. The project Codex MCP configuration is `.codex/config.toml`; its launcher `tooling/codex-scout-mcp.sh` uses `uv --offline`, loads the ignored local env file, and starts the official SDK STDIO host. Review and trust this project configuration before use. Codex configuration requires the `SCOUT_DATABASE_URL` variable to be inherited. For a local Codex CLI session, start Codex from a shell that exports the reviewed `.env`, then verify `/mcp` lists `data_mozaic_scout`:

   ```sh
   set -a
   . ./.env
   set +a
   codex
   ```

   The Codex config restricts discovery to the Scout tools and prompts for tool calls. The MCP server is also available to configured Codex CLI/IDE clients. See [Codex MCP configuration](https://developers.openai.com/codex/mcp).

## Preflight e decisão de autorização

An input URL never creates a source or grants access. An administrator must perform these steps before the agent workflow:

1. Confirm ownership or written authorization for the exact host and collection paths.
2. Add the hostname to the broker allowlist in `.local/scout-egress-domains.txt` only when needed; keep private, loopback, link-local, and reserved destination denies enabled.
3. Register a specific `source_id` and revision in `.local/authoring-policy.json`, with HTTPS origin, narrow path prefixes, GET only, finite request/time/response-byte limits.
4. Add an active approval for the exact local principal and same source/revision with equal or narrower scope and limits, and a short explicit expiry. Do not add credentials, cookies, authorization headers, or secrets.
5. Start the configured host and call `scout_preflight(source_id, revision)`. Compare its returned origin/path/limits with the requested URL and task. The Codex workflow must stop if the lookup or approval is absent, expired, broader than intended, or mismatched. Chat approval, skill instructions, and URL ownership claims do not update the local policy.

`config/scout/authoring-policy.example.json` is intentionally empty. No valid source/approval is provisioned by this repository. The JSON policy format is validated by the local loader; the concrete policy change is a separate operator action outside MCP.

## Current MCP tools

`scout_preflight`, `scout_start`, `scout_fetch`, `scout_record_finding`, `scout_propose`, `scout_status`, `scout_export_transcript`, and `scout_export_replay_fixtures` are the current tools. `scout_explore_fixture` is a separate hermetic test/demo tool and is not the HTTP source workflow. Schemas reject extra arguments; principal, policy, approval, budgets, evidence root, artifact root, and credentials are launcher-owned.

- `scout_fetch` accepts one HTTPS page, resolves source/revision from its session, reauthorizes each redirect hop, spends the durable PostgreSQL request budget, stores final response bytes in local custody, and returns bounded extracted text, links, and evidence IDs/digests. Links are suggestions; none is followed automatically.
- `scout_export_transcript` reauthorizes the session, checks proposal/finding/evidence references, verifies custody, and returns the BC14 transcript draft/v1 plus its SHA-256. The JSON transcript intentionally contains metadata and references, not raw body bytes.
- `scout_export_replay_fixtures` is the BC14 application export use case behind a thin MCP adapter. It reauthorizes the session, limits export to the proposal's evidence, verifies identity/size/digest and writes bodies below `<SCOUT_ARTIFACT_ROOT>/<session_id>/captured-fixtures/`. It does not accept an output path argument and returns only the fixture location, evidence refs, and index digest. Files are local, private test/review artifacts; preserve their source retention rules.
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
captured-fixtures/index.json
captured-fixtures/<sha256>.body
capture-playbook.yaml
validation-report.json
replay-report.json
review-summary.md
```

Save the exact `transcript_json` tool result to `transcript.json` and compare its bytes to the returned SHA-256. Use the existing BC15 CLI in that session directory:

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering draft transcript.json --output capture-playbook.yaml
uv run --locked --package mozaic-collector-engineering python -m collector_engineering validate capture-playbook.yaml
uv run --locked --package mozaic-collector-engineering python -m collector_engineering replay capture-playbook.yaml --fixtures captured-fixtures --report replay-report.json
```

The draft/v1 subset supports observed HTTPS start fetch, same-origin/in-scope link discovery, bounded page following, and raw output. There are no selectors, query pagination, credentials, arbitrary code, browser actions, or productive bot compilation. Missing fixture, digest mismatch, unsupported response, or budget failure must appear in the replay result; never repair it by fetching again. The schema and ADR 013 remain illustrative/Proposed, not runtime or release contracts.

## Validation and evidence boundaries

Run the controlled HTTPS and policy path through `make scout-http-e2e`; it requires a reviewed `.env`, Docker, local PostgreSQL, Squid 7.7, controlled DNS, and a transient TLS CA. Run the independent adversarial DNS gate with `make scout-dns-adversarial-check`; run optional sandbox topology checks with `make scout-sandbox-check`. The integration tests must not be reported as passing if skipped. See [egress sandbox operations](scout-egress-sandbox.md).

The controlled fixture includes a prompt-injection string and a fake credential marker. The host returns only bounded extracted text with the fake credential redacted, retains original bytes under custody, denies an out-of-scope path, and exports metadata-only transcript. Treat the injection as untrusted data; it cannot grant scope or authorize a request. Browser-rendered JavaScript, CAPTCHA/authentication bypass, raw redirect-body custody, production BC01/BC06/BC11, and cryptographically authenticated approval remain unsupported.

The deterministic Python/MCP test proves the platform pipeline against a controlled fixture. It does not prove Codex used the skill or interpret the source safely. A live Codex walkthrough is a separate manual demonstration and must identify its explicitly configured source/revision, active approval, limits, output location, and operator. A passing platform test must never be presented as a Codex E2E run.
