# Controlled raw artifact acquisition fixtures

These five local cases exercise the v4 `output.artifact` profile through the
existing Source/Release lifecycle, BC13 Work Intake, BC04 admission, protected
Scrapy worker, Go gateway, trusted importer, BC06 custody and BC05 publication.
The controlled source serves static fixture bytes; it has no route to external
sources. `expected/` is the independent acceptance oracle, separate from both
the source's embedded copies and the compiler fixtures.

The cases cover PDF, CSV, ZIP, non-UTF-8 binary, and JSON as a complete raw
entity. No parser runs. The ZIP is preserved without extraction and raw JSON is
preserved byte for byte, including its final newline. The gateway hashes the
`decoded-http-entity` after HTTP transport decoding; these are not TLS wire
bytes. The current live v4 profile allows one GET, one response, no redirects,
pagination, continuation, query parameters or record operations.

Run all five end-to-end cases with:

```sh
make raw-artifact-acquisition-acceptance
```

The gate independently reads each published BC05 output and retrieves its
logical artifact from BC06, then checks bytes, media type, size and SHA-256
against `expected/`. Its controlled per-response policy is 100,000 bytes and
aggregate run budget is 200,000 bytes. The v4 profile, worker handler and BC06
upload cap each bound an artifact at 2,000,000 bytes; the effective runtime cap
is the minimum of the release, policy, BC04, gateway, worker, importer and BC06
bounds. Larger artifacts and streaming are outside this increment.
