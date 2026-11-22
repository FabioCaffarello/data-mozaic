# Bounded multipage catalog

Objective: follow only the observed `a.next` links and extract the first two pages. The fixture index has three pages and records that pages 2 and 3 were discovered from `e-multi-1`. `max_pages: 2` and `max_requests: 2` require page 3 to remain unvisited. Output order is page 1 then page 2.

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering inspect-fixture examples/playbooks/multipage-catalog/fixtures https://pages.fixture.test/catalog/
uv run --locked --package mozaic-collector-engineering python -m collector_engineering try-selector examples/playbooks/multipage-catalog/fixtures https://pages.fixture.test/catalog/ 'a.next' --evidence-ref e-multi-1
uv run --locked --package mozaic-collector-engineering python -m collector_engineering validate examples/playbooks/multipage-catalog/capture-playbook.yaml --fixtures examples/playbooks/multipage-catalog/fixtures
uv run --locked --package mozaic-collector-engineering python -m collector_engineering compile examples/playbooks/multipage-catalog/capture-playbook.yaml --backend python-http --fixtures examples/playbooks/multipage-catalog/fixtures --output .local/pr10-builds/multipage-catalog
uv run --locked --package mozaic-collector-engineering python -m collector_engineering verify-build .local/pr10-builds/multipage-catalog
uv run --locked --package mozaic-collector-engineering python -m collector_engineering run-example examples/playbooks/multipage-catalog
```

Expected: two ordered records, evidence refs `e-multi-1` and `e-multi-2`, two pages/two requests, and no record from page 3. The page limit is an upper bound, not a promise to exhaust every observed link.

Limitation: all links and pages are synthetic fixture entries; this example does not test remote authorization, redirects, or live pagination drift.
