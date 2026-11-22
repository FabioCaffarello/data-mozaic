# Browser capability is unsupported

Objective: demonstrate a structurally valid request for rendered DOM. The contract and IR recognize the browser operation, but Python HTTP Reference has no `browser.rendered_dom` capability.

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering validate examples/playbooks/browser-capability-unsupported/capture-playbook.yaml --fixtures examples/playbooks/browser-capability-unsupported/fixtures
uv run --locked --package mozaic-collector-engineering python -m collector_engineering compile examples/playbooks/browser-capability-unsupported/capture-playbook.yaml --backend python-http --fixtures examples/playbooks/browser-capability-unsupported/fixtures --output .local/pr10-builds/browser
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/browser-capability-unsupported
```

Expected: validation succeeds; direct compilation fails with `CAPABILITY_UNSUPPORTED browser.rendered_dom` before creating `.local/pr10-builds/browser`; `run-example` records that rejection as the expected result. No browser is started and no fallback is attempted.

Expected records: none, because no bot build is produced. The independent acceptance oracle for this negative case is the exact capability diagnostic above.

Limitation: this demonstrates capability rejection only; it does not implement or evaluate rendered DOM behavior.
