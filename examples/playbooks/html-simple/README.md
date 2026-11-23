# HTML simple catalog

Objective: extract two product records from one synthetic HTML page. `e-html-1` identifies the fixture bytes by SHA-256. The playbook selects `article.product`; field selectors are relative to each matched article.

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering inspect-fixture examples/playbooks/html-simple/fixtures https://html.fixture.test/catalog/
uv run --locked --package mozaic-collector-engineering python -m collector_engineering try-selector examples/playbooks/html-simple/fixtures https://html.fixture.test/catalog/ 'article.product' --evidence-ref e-html-1
uv run --locked --package mozaic-collector-engineering python -m collector_engineering validate examples/playbooks/html-simple/capture-playbook.yaml --fixtures examples/playbooks/html-simple/fixtures
uv run --locked --package mozaic-collector-engineering python -m collector_engineering compile examples/playbooks/html-simple/capture-playbook.yaml --backend python-http --fixtures examples/playbooks/html-simple/fixtures --output .local/pr10-builds/html-simple
uv run --locked --package mozaic-collector-engineering python -m collector_engineering verify-build .local/pr10-builds/html-simple
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/html-simple
```

Expected: two records (`sku-7`, `sku-8`), decimal strings `12.5` and `8`, each with URL, evidence reference, and raw digest provenance. The example does not establish a live source policy or prove the catalog selector remains correct after source drift.
