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

The first three commands compare generated records to independent expectations. The browser command succeeds only when schema/semantic validation pass and compilation fails specifically with `CAPABILITY_UNSUPPORTED browser.rendered_dom`. `make collector-playbook-examples` first probes that the OS sandbox denies an external socket, then runs each example.

`run-example` uses `sandbox-exec` with network denied on macOS. On Linux it uses a non-root, read-only container with `--network none` when Docker is available, otherwise a network namespace (`unshare --net`). It fails closed when the OS cannot provide that isolation. Running an example is a local fixture replay, not source authorization or production acceptance.

To compare a separately retained run, use `collector_engineering compare EXPECTED.jsonl ACTUAL.jsonl`; the Codex authorship demonstration records a complete command sequence and independent result in its `AUTHORING-SESSION.md`.
