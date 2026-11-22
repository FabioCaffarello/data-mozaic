# CapturePlaybook draft/v2 (BC15)

Status: **draft / illustrative**. This contract is owned by BC15. It does not approve source access, create a production runtime, or release a bot. The v1 schema and behavior remain unchanged; consumers must select the version explicitly.

The v2 subset is a closed YAML mapping validated by `CapturePlaybook.schema.json` (JSON Schema 2020-12) and the BC15 semantic validator. YAML aliases, duplicate keys, arbitrary tags, unknown properties, unknown operator versions, and executable values are rejected.

## Operations

| Operator | Input → output | Semantics and effect |
|---|---|---|
| `http.fetch/v1` | URL → `HtmlDocument` or `JsonDocument` | In this draft backend, resolves an exact URL from a digest-verified local fixture index. It never performs a network request. |
| `links.discover/v1` | HTML document → `LinkSet` | Extracts same-scope `href` values selected by CSS. It records candidates; the next operation is still bounded by fixture availability and scope. |
| `pagination.bounded/v1` | `LinkSet` → `PageSet` | Visits observed links in source order up to `max_pages`; `stop_when_no_links` must be true. General cycles are forbidden. |
| `select.html_records/v1` | HTML document/page set → `HtmlRecords` | Applies CSS to each document. Field selectors are relative to each matched record. Cardinality is checked over the selected sequence. |
| `select.json_records/v1` | JSON document → `JsonRecords` | Expands exactly one final `/*` in a restricted JSON Pointer-like path; no filter, slice, recursive descent, or expression. |
| `browser.render_dom/v1` | document → `RenderedDocument` | Drafted capability request only; accepted by schema and IR, unsupported by the Python HTTP Reference backend. |
| `select.rendered_dom_records/v1` | rendered document → records | Drafted browser capability request only; no backend implementation in this increment. |
| `extract.fields/v1` | selected records → typed records | Maps text/attribute/JSON scalar values to `string`, `integer`, `decimal`, or `boolean`; supported normalization is `trim` and `collapse_whitespace`. |
| `output.records/v1` | typed records → JSONL/report | Emits canonical JSONL plus a separate execution report. Each record carries evidence URL/ref and raw digest provenance. |
| `output.raw/v1` | documents → raw references | Emits metadata references only; raw bytes remain under the custody owner. |

Budgets bound requests, pages, response bytes, records, and total output bytes (including provenance and reports). Field samples and normalized scalar outputs are bounded as well. Decimal values are serialized as JSON strings in normalized base-10 form, avoiding binary-float ambiguity. Required values that are absent, non-scalar, or not strictly convertible fail with a typed diagnostic. The only supported failure policies in v2 are explicit `fail` policies.

The IR version is `collector-ir/v1`. The Python HTTP Reference capability profile supports fixture fetch, HTML CSS selection, restricted JSON path, fixture link discovery, bounded fixture pagination, record extraction, JSONL, and raw references. Missing capabilities fail compilation before build publication.

Validate schema and semantics with:

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering validate PLAYBOOK --fixtures FIXTURES
```

Fixture coverage means the referenced fixture digest is verified and a declared top-level selector/path is exercised against fixture data. It is not proof that selected elements are the intended business records. Review samples and compare against independently authored expected records.
