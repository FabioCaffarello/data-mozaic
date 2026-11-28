# CapturePlaybook drafts (BC15)

This package creates an illustrative BC15 candidate from a host-produced transcript and validates a closed YAML subset. The protected Scout MCP composition runs offline replay/acceptance in an isolated executor; the package does not browse, execute a capture, approve a revision, or create a `BotRelease`.

## Local offline commands

```sh
make sync
make acquisition-capabilities
tooling/codex-scout-bc15.sh draft /absolute/session/transcript.json --output /absolute/session/candidate.yaml
tooling/codex-scout-bc15.sh validate /absolute/session/candidate.yaml
```

`make acquisition-capabilities` prints the versioned surface projected from
BC15 backend profiles, Playbook schemas and the live compiler's constrained
graphs. It is descriptive only: it grants neither source access nor release
approval. Historical Playbook schema versions retain their original meaning.

## CapturePlaybook draft/v2 and offline compiler backends

The v1 `draft` command and protected raw replay/acceptance behavior remain unchanged. For synthetic fixture authoring, v2 adds structured field extraction, Typed IR, code generation, and local build verification:

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering inspect-fixture FIXTURES URL
uv run --locked --package mozaic-collector-engineering python -m collector_engineering try-selector FIXTURES URL 'CSS_SELECTOR' --evidence-ref EVIDENCE_REF
uv run --locked --package mozaic-collector-engineering python -m collector_engineering validate PLAYBOOK_V2 --fixtures FIXTURES
uv run --locked --package mozaic-collector-engineering python -m collector_engineering compile PLAYBOOK_V2 --backend python-http --fixtures FIXTURES --output BUILD_DIR
uv run --locked --package mozaic-collector-engineering python -m collector_engineering compile PLAYBOOK_V2 --backend scrapy-fixture --fixtures FIXTURES --output BUILD_DIR
uv run --locked --package mozaic-collector-engineering python -m collector_engineering verify-build BUILD_DIR
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example EXAMPLE_DIR
make collector-cross-backend-conformance
```

`inspect-fixture` verifies the fixture digest and returns metadata plus minimized structure; it omits document values by default. `try-selector` returns bounded samples explicitly marked as untrusted data and does not claim that matches are semantically correct. Validation reports selector/path coverage separately from structural validity. The expected-record file is independently authored and compared by `run-example` after network-isolated execution.

The Python HTTP Reference and Scrapy Fixture backends resolve only exact URLs in the supplied fixture index and verify fixture identity and digest. Scrapy's HTTP/HTTPS handlers are closed to fixtures; standard network download handlers, other schemes, retries, redirects, extensions and feeds are disabled. On macOS the local sandbox uses `sandbox-exec` with network denied and CPU/time limits; this is reported as less restrictive than Linux. On Linux, execution requires Docker with `--network none`, read-only root, non-root UID, restricted mounts and resource limits; there is no network namespace fallback. If the sandbox is unavailable, the command fails closed. The bot container/process receives no Docker socket or credentials. This offline demonstration does not authorize a source or provide production capture.

The mandatory example target first performs an external-socket denial probe inside the selected OS sandbox and fails if the probe succeeds:

```sh
make collector-playbook-examples
```

The versioned wrapper resolves the monorepo from its own location and works with an arbitrary current directory and artifact paths containing spaces. It requires the `.venv` produced by `make sync`, skips environment synchronization, stays offline, and keeps uv's cache in the ignored `.local/uv-cache/` path inside the checkout. This lets Codex workspace-write run it without access to a user-global cache. If the wrapper fails, stop and report the failure; do not unset proxy variables or invoke BC15 through `.venv`/Python as a fallback. The transcript must include an approval receipt, exact source revision and scope, a Scout v1 `PlaybookProposal`, classified findings, durable evidence references, and explicit BC14 navigation identity. For draft/v1 the proposal capability vocabulary is `http.fetch`, `links.discover`, `pagination.bounded`, and `output.raw`. BC14 transcript draft v2 carries requested/effective start URLs, ordered observations, evidence identity and discovery relations. Transcript draft v1 remains readable for inspection but BC15 refuses to author from it because its start URL is ambiguous. The local CLI checks that values match; it cannot authenticate the receipt. Only a trusted policy host can establish identity and authorization.

The protected MCP executor receives an authorized transcript, draft and custody-verified fixture bytes over a bounded channel. BC15 independently verifies fixture identity, size and digest; it makes no network calls, records pages/request budgets, and publishes `output.raw` into the executor's per-run named volume. The container has no network, Docker socket, host bind mount, capabilities, or writable root filesystem; it runs as non-root with CPU, memory, PID and wall-time limits. Docker Desktop stores the volume in its Linux VM. Linux CI runs containers in the host kernel, so this does not claim protection from kernel compromise or container escape. The Codex workspace-write process is denied the local Docker daemon socket; the MCP adapter exposes only typed replay/acceptance operations and does not accept output/fixture paths, commands, mounts or runtime options. The underlying direct Python `replay()` and `accept_acquisition()` functions remain for trusted tests/development callers only. Their filesystem writer rejects initial symlinks and checks digests, permissions, and publication integrity, but `O_NOFOLLOW`, descriptor-relative operations, `fstat`, and post-write checks cannot prevent a same-UID process from moving an already-open directory. Direct calls are not the protected mode. Raw output preserves bytes and does not extract structured records. Missing fixtures, invalid status, excess bytes, budget exhaustion, output failures or digest mismatch fail closed; no `passed` result is returned after a failed publication.

## Current transcript and raw document handoff

New controlled HTTP exports use transcript draft/v3, which adds the observed
`content_type` declaration to each page; v1/v2 remain readable with their
historical shape. Scout discovery accepts bounded HTML, plain text, JSON text,
and opaque PDF. JSON text is redacted and bounded for authoring. PDF bytes are
not parsed or returned as text; BC06 custody remains the byte authority. A v3
single-page transcript can produce a v4 raw artifact candidate:

```sh
tooling/codex-scout-bc15.sh draft /absolute/session/transcript.json \
  --profile raw-artifact-v4 --output /absolute/session/candidate-v4.yaml
tooling/codex-scout-bc15.sh validate /absolute/session/candidate-v4.yaml
```

The adapter binds the evidence's declared media type and the minimum of the
approved response budget and historical 2,000,000-byte v4 maximum. It rejects
multiple pages, redirects, malformed media declarations and oversize evidence.
This only creates a candidate; it does not promote a Playbook, compile a live
BotBuild, create a BotRelease or approve authority. The protected Scout broker
deployment and governed promotion path remain separate operational gates.

## Protected run lifecycle

The local broker stores run ownership and allowlisted reports in a private SQLite registry at `~/.local/state/data-mozaic/scout-bc15/bc15-runs.sqlite3`, outside the Codex artifact directory. It is adapter state, not BC15 domain state, so it does not add tables to Scout's PostgreSQL schema. The registry records run ID, named volume, principal, Scout session, state, creation/expiry timestamps and a report capped at 256 KB. SQLite is used because this local broker needs a small durable index available before the MCP server starts; it is not a distributed service or a production custody store.

Each output volume is labelled with the fixed executor owner and run ID. The broker reconciles only matching `dm-bc15-<run-id>` volumes after restart. Interrupted runs become `incomplete`; labelled volumes missing from the index become orphaned/incomplete entries. Completed and incomplete artifacts are retained for 24 hours. Cleanup checks every 60 seconds and on broker startup/status/new run, verifies the volume name and both labels, then removes only that volume; repeated cleanup is safe. No MCP operation accepts a volume name or removes artifacts. The broker caps retained runs/volumes at 100, registry rows at 200, concurrent execution at two, each request at 48 MB and verified fixture bodies at 24 MB aggregate, with each body at 2 MB and a 60-second worker timeout. At the configured request cap, raw output is bounded by those fixture limits; Docker does not enforce a physical per-volume quota in this local composition.

If Docker inventory fails, labels do not match, or a volume cannot be safely removed, cleanup leaves it in place and retains its registry record. The broker caps registry rows at 200 and prunes the oldest expired tombstones when needed; if only live or incomplete records remain, new runs fail closed. Host administrators and Docker daemon administrators remain able to inspect or remove these volumes. The SQLite registry and volume contents are local development artifacts and are not encrypted at rest.

Acquisition acceptance is a separate gate. It compares the draft/replay with an independently authored expectation file that names source/revision, requested/effective start, required pages and digests, required navigation edges, content markers and limits. The expectation file must not be generated from the playbook. Acceptance can prove fixture-specific page coverage and raw-content markers, but draft/v1 cannot perform structured record extraction.

Draft DSL: [CapturePlaybook draft v1](../../contracts/capture-playbook/draft/v1/CapturePlaybook.schema.json). Its schema and ADR 013 are Proposed/illustrative; the protected local BC15 executor consumes it for offline replay/acceptance, but it is not an approved production runtime contract. The local Codex workflow requires the controlled MCP host, active source approval, protected executor image, and broker for replay. See [Scout tool host operations](scout-authoring-tools.md).
