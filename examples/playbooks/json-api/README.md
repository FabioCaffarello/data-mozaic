# Synthetic JSON API

Objective: expand the single bounded `/items/*` array and strictly convert quantity, decimal price, and boolean values.

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering inspect-fixture examples/playbooks/json-api/fixtures https://api.fixture.test/v1/items
uv run --locked --package mozaic-collector-engineering python -m collector_engineering validate examples/playbooks/json-api/capture-playbook.yaml --fixtures examples/playbooks/json-api/fixtures
uv run --locked --package mozaic-collector-engineering python -m collector_engineering compile examples/playbooks/json-api/capture-playbook.yaml --backend python-http --fixtures examples/playbooks/json-api/fixtures --output .local/pr10-builds/json-api
uv run --locked --package mozaic-collector-engineering python -m collector_engineering verify-build .local/pr10-builds/json-api
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/json-api
```

Expected: two records; `quantity` is integer, `enabled` is boolean, and decimal `unit_price` is a canonical JSON string. No generic JSONPath expressions are supported.

Limitation: the synthetic JSON document is already captured; this does not exercise API authentication, remote authorization, or schema drift.
