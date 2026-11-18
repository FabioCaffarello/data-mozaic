# Scout MCP PR #7 closure — audit, plan and gates

Audit date: 2026-11-13. This is the closure plan for the existing local, uncommitted work on `feat/scout-mcp-tool-host`.

## Observed initial state

- `pwd`: `/Volumes/OWC Express 1M2/Develop/data-mozaic`.
- Branch: `feat/scout-mcp-tool-host`, HEAD `d9fb5cc`, same commit as `main` and `origin/main`; no branch commits. Existing unstaged changes: README, Scout app/dependency/lock, application, repository, fakes, ports, policy, runbook, roadmap, architecture checker, host package, prior readiness assessment and MCP/incremental tests. No staged changes. The earlier report's untracked ZIP is absent now; no cleanup was performed.
- `gh pr view 7` reports no PullRequest; `gh pr list --state all` lists merged PRs #1–#6 only. Thus there is no existing PR #7 to update.
- PR #6 is merged from `feat/scout-tool-host-sandbox` into `main`; local main remains at its merge commit.
- Docker CLI is 28.5.1, Compose v2.40.3, context `desktop-linux`; `DOCKER_HOST`/`DOCKER_CONTEXT` are unset. Client commands cannot connect to the Desktop socket and launchd shows only the installed helper, not a running Desktop daemon. No context changes or volume actions were made.
- Existing tool server has five tools. It uses the official low-level MCP `Server` over STDIO and closed schemas. `scout_start` currently derives its command/session ID from the client request ID. The local composition injects `_DisabledCustody`; no fixture exploration, durable evidence write, or transcript export is reachable from MCP.
- BC14 already owns aggregate observations/manifests/findings/proposal and command receipt/outbox tables. Migration `001` supports `in_progress`, `uncertain`, and `completed` command receipts. `save()` currently updates every session receipt, so a per-command transactional CAS/receipt method is needed. No historical migration may be changed.
- BC15 resolves JSON schemas from the monorepo root at import time. `mozaic-collector-engineering` wheel therefore fails when installed outside the checkout. The current transcript parser expects `approved_scope`, `approval_record`, `proposal`, `findings`, and `pages`; its approval record is only locally comparable, not cryptographically authenticated.

## Planned patch set (before implementation)

| Files | Change | Invariants affected |
|---|---|---|
| `apps/scout/scout/application.py`, `ports.py`, `domain.py` only if necessary | Add authorized fixture exploration, custody verification, observation/manifest creation, command replay, and canonical transcript projection as BC14 use cases | Fixed source/revision and effective scope; principal from trusted composition; evidence exists and verifies before manifest seal; findings/proposals reference same-session manifests; no automatic independent verification; authorization precedes snapshot/receipt reads |
| `apps/scout/scout/adapters/postgres/repository.py`, `migrations/scout/007-*.sql` only if existing receipt columns cannot support atomic per-command CAS | Add per-command receipt begin/replay/complete/uncertain operations with canonical fingerprints and CAS/outbox commit. Prefer existing receipt schema; any new migration is checksummed and tested | No external work inside SQL transaction; same key+fingerprint replays; changed args conflict; concurrent writes have one CAS winner; proposal and receipt persist atomically |
| `apps/scout/scout/adapters/local_custody.py`, `adapters/fixtures.py` or equivalent cohesive modules | Add explicitly test/reference-only atomic custody under configured external directory and server-owned fixture catalog; reject unknown fixture IDs and symlinks/path traversal | BC06 remains owner; identity is session-bound and distinct from content digest; bytes verified on lookup; no arbitrary path input or public network |
| `apps/scout/scout/tool_host/server.py`, `composition.py`, `__main__.py` | Preserve five existing tools; add `scout_explore_fixture` and `scout_export_transcript`; derive principal and deterministic idempotency from trusted host context/canonical command fingerprint | MCP adapter has no domain transitions; no model-selected principal/approval/budget/path/URL; STDOUT protocol-only; outputs bounded and redacted |
| `contracts/scout/transcript/draft/v1/`, fixtures | Define the draft BC14→BC15 DTO, canonical JSON/hash and valid/invalid examples without altering Scout v1 or BC15 playbook draft v1 | Explicit owner/version/status; source/session/evidence cross-links validated; deterministic ordering; no raw bytes/secrets; proof limits explicit |
| `apps/collector-engineering/collector_engineering/schemas/`, `playbooks.py`, `pyproject.toml` | Package CapturePlaybook, Scout proposal and transcript schemas via `importlib.resources`; parity-test copies against canonical contracts; preserve CLI behavior | BC15 consumes a public DTO, not BC14 models; contract copies cannot silently drift; wheel and sdist work from external CWD |
| `tests/test_scout_*.py`, `tests/integration/test_postgres_scout.py`, packaging test tooling | Add hermetic MCP→fixture→custody→transcript→BC15 draft/validate/replay, malicious args, corruption, authorization, idempotency/restart/CAS and isolated package install tests | No public network; required PG integration has zero skips for GO; corruption and cross-session refs fail closed |
| README, operations runbooks, roadmap, this assessment; proposed ADR only if required | Document local-only adapters, SDK compatibility, transcript proof limits, setup, gate results and deferrals | No ADR auto-acceptance; no claim of agent sandbox or production BC01/06/11/15 |

## Test matrix

| Area | Required evidence |
|---|---|
| MCP STDIO | Official client initialize/list/call/error/shutdown; schemas include `additionalProperties:false`; extra principal/call/approval/budget/path fields rejected |
| Authorization | Unknown source/revision, wrong principal, expired/revoked/missing approval and widening denied before side effects; status/export reauthorize before receipt/snapshot |
| Fixture/exploration | Only registered fixture IDs; no socket opens; URL/path/symlink/path traversal arguments are not accepted; fixture body treated as untrusted data |
| Evidence custody | Atomic durable store, host-computed SHA-256 and size, identity lookup after uncertain ACK/restart, byte corruption denied; evidence references cannot cross session/source |
| Commands/state | Stable server-derived key/fingerprint, replay same result, changed payload conflict, expected-version CAS, concurrent duplicate has one aggregate effect, fresh process recovers uncertain custody |
| Transcript | Same snapshot yields byte-identical canonical JSON/hash; altered digest/approval/evidence/source/proposal rejected; incomplete/in-progress session cannot export |
| BC15 | Build wheel + sdist, install in isolated temp environment outside repo with external CWD, draft/validate/replay fixture; output remains draft |
| Repository gates | `uv lock --check`, `make check`, `make ci`, package/import/boundary tests, `git diff --check`, PG up/status/migrate twice/recovery with zero required skips, Compose config and sandbox check when Docker is available |

## GO rule

The local machine currently has no Docker daemon. Do not substitute fakes for required PostgreSQL or network-isolation evidence. If those gates cannot be run, record NO-GO, preserve all work, do not commit/push/open a PR, and report exact blockers. PR #8 remains HTTP-authorized navigation; PR #9 remains Codex workflow/agent containment.

## Acceptance matrix

| Gate | Evidence obtained | Does not prove / outstanding |
|---|---|---|
| Repo/Git | Branch `feat/scout-mcp-tool-host`, HEAD `d9fb5cc`, staged diff empty, unstaged/untracked work preserved; `gh pr view 7` returns no PR | PR #7 has not been opened; no commits, push or remote CI exist |
| Tool host | Official MCP client over STDIO initializes, lists closed schemas, executes the full fixture workflow and shuts down; protocol test would fail if stdout were polluted; safe tool errors/logging omit arguments and payload | Does not sandbox Codex or a local agent |
| Identity/security | Principal is launcher-bound local reference; source revision and approved scope are fixed; policy is rechecked before snapshot/status/export reads; unit cases deny wrong principal, revision, expiry, revocation, scope expansion and exhausted budget | Does not provide productive multi-user authentication or BC11 integration |
| Evidence | Fixture bytes are stored by the local custody double with separate artifact identity, SHA-256 and size; corruption, concurrent replay, lost ACK retry and reconciler behavior have hermetic coverage | Does not prove productive BC06 custody or inventory |
| Incremental commands | Server-derived command key/fingerprint, durable receipt operations, CAS, same-key replay and concurrent duplicate behavior are implemented; PostgreSQL restart/fault-injection tests were added | Does not prove global exactly-once effects; PostgreSQL-backed tests remain unexecuted |
| Transcript | Versioned DTO, canonical UTF-8 serialization, stable digest, source/session/manifest/finding/proposal checks, redaction and approval/hash/reference tamper tests pass | Does not establish an executable bot |
| BC15 packaging | Wheel and sdist install outside checkout; schema parity, transcript schema validation, CLI draft/validate and fixture replay pass | Does not make a draft a production release |
| PostgreSQL | Existing and new migration/restart/CAS/fault tests are present; `make ci` reports 19 skipped PG integration cases; explicit DB targets failed | Required double migration, restart/fault tests with zero skips have not passed |
| Docker/sandbox | Compose config validation passed | Docker daemon was unavailable; no healthy Compose DB or real egress test ran; neither would isolate the local agent shell |
| PR | Local hermetic checks passed and blockers are recorded | GO local is false, so no commits, Draft PR or remote CI were created; no architectural approval or merge is implied |

### Restaurar o daemon para repetir os gates

No macOS com Docker Desktop instalado, inicie o Desktop pelo Finder ou execute `open -a Docker`; aguarde o estado “Engine running”. Mantenha o contexto atual e confirme `docker context show` e `docker info`. `docker info` deve listar tanto `Client` quanto `Server`; se continuar sem conexão, verifique o estado do Docker Desktop e o socket/contexto já selecionado, sem remover volumes ou trocar contexto às cegas. Depois execute, nessa ordem: `make db-up`, `make db-status`, `make db-migrate` duas vezes, `make test-integration`, `make test-recovery` e `make scout-sandbox-check`. Exija zero skips nos testes PostgreSQL e registre os resultados antes de reconsiderar GO. `docker compose ... config -q` sozinho valida somente a configuração.

## Implementation and gate results (2026-11-13)

The file plan above was recorded before the first implementation patch. Work is on the pre-existing local branch `feat/scout-mcp-tool-host`, which points at the same commit as `main`; PR #6 is already merged and no PR #7 exists remotely. No commit or push has been made.

Implemented in the working tree:

- BC14 application operations now start sessions without invoking the S1 demo, explore server-known fixture IDs, persist command intent/result receipts, apply expected-version CAS, validate custody before sealing a manifest, append findings, submit proposal/outbox together, and return reauthorized status/transcript projections. Existing aggregate rules continue to block `independently_verified` findings.
- The MCP server retains all five prior tools and adds `scout_explore_fixture` and `scout_export_transcript`. Schemas are closed, input JSON is capped at 32 KB, transcript output at 256 KB, and server-derived keys/fingerprints do not use MCP request IDs as authentication or authorization.
- `LocalFileEvidenceCustody` is a reference-only BC06 double. It atomically stores content-addressed bytes and immutable identity receipts outside the checkout, verifies digest/size, and permits identity lookup for uncertain acknowledgement recovery. The fixtures are package-owned; the adapter never opens network sockets.
- The new draft DTO is `contracts/scout/transcript/draft/v1/ScoutSessionTranscript.schema.json`. Host export reauthorizes, refuses incomplete commands, verifies custody and cross-context references, sorts collections and emits canonical compact UTF-8 JSON with newline. The tool returns its SHA-256 separately. Raw bytes, page text and credentials are omitted. The approval record is a locally comparable reference and is not cryptographically authenticated.
- BC15 loads packaged schema resources through `importlib.resources`; canonical-contract parity, wheel install and sdist install are tested in temporary environments from an external CWD without checkout `PYTHONPATH`.
- No migration or existing Scout v1 / CapturePlaybook v1 schema was changed. One new transcript draft contract and Proposed ADR 015 were added. MCP resolves to 1.30.0 under `mcp>=1.30,<2`; v2 conversion is deferred until its handshake/client compatibility has an explicit gate.

Hermetic commands already executed successfully:

- `make ci` — final exit 0: repository checks passed; 76 repository tests passed and 19 required PostgreSQL integration tests were skipped because `SCOUT_TEST_DATABASE_URL` is not configured; 4 observability tests passed. This does not satisfy the zero-skip database gate.
- `uv run --locked --package mozaic-scout python -m unittest tests.test_scout_contracts tests.test_scout_incremental tests.test_scout_mcp_stdio tests.test_collector_engineering tests.test_collector_engineering_packaging -v` — final targeted run had 22 hermetic tests pass. Includes the SDK STDIO E2E and isolated BC15 packaging tests.
- The MCP integration launches a real SDK STDIO server and client, executes two fixture explorations, findings, proposal/outbox state, deterministic double export, BC15 `draft`, `validate`, and fixture `replay`.
- `uv build --package mozaic-scout` and `uv build --package mozaic-collector-engineering` — both wheel and sdist built; wheel contents include the Scout host/fixtures and all three BC15 schemas.
- Packaging test builds wheel and sdist, installs both offline in temporary environments outside the checkout with no checkout `PYTHONPATH`, compares embedded schema hashes with canonical contracts, and runs all three CLI operations.
- `make ci` includes the final `make check` equivalent and passed lint, docs, migration-name, naming, architecture, unit, MCP and packaging checks.
- `docker compose ... config -q` passed configuration validation only. `make db-up` and `make db-status` failed to connect to the Docker Desktop daemon. Both `make db-migrate` attempts exited non-zero without a successful migration result; `make test-integration` and `make test-recovery` each exited non-zero before running a PostgreSQL test suite. `make scout-sandbox-check` also failed because the daemon was unavailable. No context or volume was changed. Compose configuration validation does not prove network isolation.

**Decision at the initial closure snapshot: NO-GO for commit/push/Draft PR**. The daemon was unavailable in that run, so Docker/PostgreSQL gates had not passed. The following final review supersedes that environment-specific result.

## Final pre-publication revalidation (2026-11-13)

- `git fetch origin` succeeded. Branch remains `feat/scout-mcp-tool-host`; `HEAD` and `origin/main` are both `d9fb5cca54537414a1ed97cb9c7128186ed0bec1`; branch divergence is `0/0`. The index was empty before staging. `.env` and `.venv` are ignored local files and were excluded; neither was modified or added.
- The Docker daemon is now available (`docker info`, context `desktop-linux`). `make db-up` reached healthy PostgreSQL; `make db-status` reported healthy; `make db-migrate` completed twice with migrations current.
- `make test-integration` and `make test-recovery` each passed all 19 PostgreSQL tests with zero skips. The migration checksummed/idempotency, concurrent CAS, fresh-process restart and injected failure recovery cases ran against the configured local DSN via the Make targets.
- `make scout-sandbox-check` passed against Docker: direct TCP/DNS egress was blocked and the proxy denied private-IP and unregistered-source targets. This verifies the Scout worker topology probe only; it does not isolate the local agent shell.
- `make check` and `make ci` passed. The aggregate offline suite ran 76 repository tests with 19 PostgreSQL skips because the CI targets do not load `.env`; these skips are covered separately by the zero-skip explicit PostgreSQL runs above. Four observability tests passed. Ruff, format, docs, migration names, naming and architecture checks passed. `git diff --check` and cached diff check passed.
- The existing hermetic test run covered official MCP SDK STDIO initialize/list/call/shutdown, incremental fixture/custody/transcript flow, authorization denials, transcript validation and BC15 draft/validate/replay. Packaging tests install wheel and sdist outside the checkout and compare packaged schema hashes to canonical contracts. Both Scout and collector-engineering wheel/sdist builds passed in the prior recorded run; package tests passed again under `make ci`.
- `gh pr list --state all` shows merged PRs #1–#6 only; no PR exists for this branch. No commit, push, merge or PR creation had occurred as of this evidence capture.

**Final local decision: GO for conventional commits, normal push and one Draft PR.** Remote CI remains unobserved until publication. Approval of Proposed ADR 015 and human architecture/security review remain outstanding; no approval or merge is implied. Public HTTP navigation, productive BC06/BC11/BC15 integrations, Codex process isolation and executable bot release remain outside this PR.
