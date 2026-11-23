# Executable CapturePlaybook examples

These examples use synthetic sources and local fixtures. Their `expected-records.jsonl` files were authored from the fixture contents and contract independently of the compiler output.

Run a complete validate → compile → verify → no-network execution → independent compare flow:

```sh
make sync
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/html-simple
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/multipage-catalog
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/json-api
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/browser-capability-unsupported
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/codex-authorship-demo
```

The first three examples are compiled to both Python Reference and Scrapy Fixture builds by the cross-backend target and compared to independent expectations, including JSONL order and provenance. The browser command succeeds only when schema/semantic validation pass and both capability checkers reject `browser.rendered_dom`. `make collector-playbook-examples` first probes that the OS sandbox denies an external socket, then runs the Python Reference examples; `make collector-cross-backend-conformance` runs both engines.

`run-example` uses `sandbox-exec` with network denied on macOS. That is classified as a less restrictive local sandbox. On Linux it requires Docker and uses a non-root, read-only container with `--network none`, restricted read-only mounts, CPU/memory/PID and timeout limits; it has no network-namespace fallback. It fails closed when that isolation is unavailable. Running an example is a local fixture replay, not source authorization or production acceptance.

To compare a separately retained run, use `collector_engineering compare EXPECTED.jsonl ACTUAL.jsonl`; the Codex authorship demonstration records a complete command sequence and independent result in its `AUTHORING-SESSION.md`.
