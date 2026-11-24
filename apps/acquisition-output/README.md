# BC05 Acquisition Output foundation

This package owns the datastore-independent `CaptureOutput` variants, `OutputSet` lifecycle, application use cases and consumer-owned ports. It is not a worker, database adapter or artifact custody service. Public JSON projections live under `contracts/acquisition-output/draft/v1/`; Python domain objects are private and explicitly mapped by `wire.py`.

The `OutputSetRepositoryPort` returns a bounded `OutputSetSnapshot`; member records are not embedded in operational responses. The public draft `OutputSetManifest` projection remains a separate wire contract that includes bounded membership entries. `stream_manifest_digest` consumes sorted member entries incrementally and preserves the existing v1 digest bytes. This lets a future adapter stream the membership cursor at seal without building a second full member list; the adapter must still enforce stage/seal serialization with its datastore transaction boundary.

The `InMemoryOutputStore` reference adapter serializes commands under one process lock and stores output members separately from the bounded OutputSet aggregate. It demonstrates idempotent staging, stage-versus-seal ordering, immutable membership after seal, exact manifest verification, and publication visibility. It loses all state on process exit and provides no cross-process uniqueness, transaction log, outbox, persistence recovery, or BC04/BC05 distributed finalization protocol.

Payloads and manifest intent use the RFC 8785 JSON Canonicalization Scheme (`rfc8785` package). Accepted values are bounded I-JSON: object keys are non-empty strings, strings preserve Unicode code points without normalization, null is retained, object ordering follows JCS, numbers are finite IEEE-754 values and true integers are restricted to ±(2^53−1), and Decimal, bytes, non-finite numbers, lone surrogates, duplicate JSON keys at a parser boundary, and unsupported Python objects are rejected. High precision decimals/identifiers must be contract-defined strings. RFC 8785 and BC05 vectors are checked in `contracts/acquisition-output/draft/v1/canonicalization-vectors.json`.

`ArtifactRef` is a logical BC06 identity plus digest/size and optional declared media type. It contains no physical URI or access token. The application requires a custody port to return a matching tenant/artifact/digest/size receipt before staging artifact-bearing output. A structurally valid reference alone is not proof of custody.

`capture_output_from_json` is the strict raw-JSON entry point; it rejects duplicate properties before the object mapper, invalid UTF-8 and non-finite JSON constants. `capture_output_from_wire` accepts already-decoded mappings and cannot recover duplicate-key information lost by a permissive decoder.

Use `make acquisition-output-tests` for the package suite. This initial increment has no MongoDB, PostgreSQL output persistence, MinIO, BC06 implementation, live collector, or worker integration.
