# CapturePlaybook drafts (BC15)

This package creates an illustrative BC15 candidate from a host-produced transcript, validates a closed YAML subset, and replays it using captured fixture bytes. It does not browse, execute a capture, approve a revision, or create a `BotRelease`.

## Local offline commands

```sh
uv sync --all-packages --locked
uv run --package mozaic-collector-engineering python -m collector_engineering draft transcript.json --output candidate.yaml
uv run --package mozaic-collector-engineering python -m collector_engineering validate candidate.yaml
uv run --package mozaic-collector-engineering python -m collector_engineering replay candidate.yaml --fixtures captured-pages --report replay-report.json
```

The transcript must include an approval receipt, the exact source revision and scope snapshot, a Scout v1 `PlaybookProposal`, classified findings, and durable evidence references. The local CLI checks that values match; it cannot authenticate the receipt. Only a future trusted tool host can establish identity and authorization.

Replay reads `index.json` and body files below the fixture directory. It verifies every body digest and source scope, makes no network calls, and reports missing captured pages, invalid status, excess bytes, and budget exhaustion as failures. It never follows a URL absent from the captured fixture index. A passing replay establishes fixture consistency only.

Draft DSL: [CapturePlaybook draft v1](../../contracts/capture-playbook/draft/v1/CapturePlaybook.schema.json). Its schema and ADR 013 are Proposed/illustrative; no runtime consumes this format. The Codex skill stops if there is no controlled tool host and verifiable process/network isolation. See [Scout tool host operations](scout-authoring-tools.md).
