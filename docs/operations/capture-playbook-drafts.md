# CapturePlaybook drafts (BC15)

This package creates an illustrative BC15 candidate from a host-produced transcript, validates a closed YAML subset, and replays it using captured fixture bytes. It does not browse, execute a capture, approve a revision, or create a `BotRelease`.

## Local offline commands

```sh
make sync
tooling/codex-scout-bc15.sh draft /absolute/session/transcript.json --output /absolute/session/candidate.yaml
tooling/codex-scout-bc15.sh validate /absolute/session/candidate.yaml
tooling/codex-scout-bc15.sh replay /absolute/session/candidate.yaml --fixtures /absolute/session/captured-fixtures --output /absolute/session/replay-output --report /absolute/session/replay-report.json
tooling/codex-scout-bc15.sh accept /absolute/session/candidate.yaml /absolute/session/transcript.json --fixtures /absolute/session/captured-fixtures --expectations /absolute/approved/acceptance.json --output /absolute/session/acceptance-output --report /absolute/session/acquisition-acceptance.json
```

The versioned wrapper resolves the monorepo from its own location and works with an arbitrary current directory and artifact paths containing spaces. It requires the `.venv` produced by `make sync`, skips environment synchronization, stays offline, and keeps uv's cache in the ignored `.local/uv-cache/` path inside the checkout. This lets Codex workspace-write run it without access to a user-global cache. If the wrapper fails, stop and report the failure; do not unset proxy variables or invoke BC15 through `.venv`/Python as a fallback. The transcript must include an approval receipt, exact source revision and scope, a Scout v1 `PlaybookProposal`, classified findings, durable evidence references, and explicit BC14 navigation identity. For draft/v1 the proposal capability vocabulary is `http.fetch`, `links.discover`, `pagination.bounded`, and `output.raw`. BC14 transcript draft v2 carries requested/effective start URLs, ordered observations, evidence identity and discovery relations. Transcript draft v1 remains readable for inspection but BC15 refuses to author from it because its start URL is ambiguous. The local CLI checks that values match; it cannot authenticate the receipt. Only a trusted policy host can establish identity and authorization.

Replay reads a complete `index.json` and body files below the fixture directory. It verifies every body digest and source scope, makes no network calls, records pages and request budgets, and materializes the declared `output.raw` bodies under the explicit output directory. Raw output preserves response bytes; it does not extract structured records. Missing fixtures, invalid status, excess bytes, budget exhaustion, output failures or digest mismatch fail replay. A `passed` replay only means declared operations ran against the available offline fixtures.

Acquisition acceptance is a separate gate. It compares the draft/replay with an independently authored expectation file that names source/revision, requested/effective start, required pages and digests, required navigation edges, content markers and limits. The expectation file must not be generated from the playbook. Acceptance can prove fixture-specific page coverage and raw-content markers, but draft/v1 cannot perform structured record extraction.

Draft DSL: [CapturePlaybook draft v1](../../contracts/capture-playbook/draft/v1/CapturePlaybook.schema.json). Its schema and ADR 013 are Proposed/illustrative; no runtime consumes this format. The local Codex workflow requires the controlled MCP host, active source approval, and broker for the implemented fetch path. It explicitly does not claim process/shell isolation; see [Scout tool host operations](scout-authoring-tools.md).
