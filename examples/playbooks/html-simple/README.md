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

To run the durable, fixture-only platform flow, start the local PostgreSQL, MongoDB and BC06/RustFS services from their owning development setups, then run from the repository root:

```sh
make execution-coordination-fixture-acquisition \
  MONGO_ENV_FILE=../data-mozaic-mongodb/.env.mongodb \
  BC06_ENV_FILE=../data-mozaic-artifact-custody/.env.artifact-custody
```

The command compiles and verifies the Scrapy BotBuild, executes its Linux worker with `--network none`, validates the bounded result bundle, confirms the synthetic fixture body through BC06, publishes CompositeOutputs through BC05 using the local fixture authorization profile, and exercises BC04 reconciliation and fresh-process replay. It stores run outputs and immutable assembly copies under ignored `.local/execution-coordination-fixture/` for recovery inspection. The example is synthetic offline evidence; the artifact bytes are the declared fixture response-body bytes, not original HTTP wire bytes or a live-source capture. It does not authorize live networking.
