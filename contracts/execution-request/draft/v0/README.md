# ExecutionRequest draft v0

Internal, fixture-only process-boundary contract owned by BC04 Execution Coordination and consumed by the trusted BC05 Scrapy adapter/worker. This schema is draft and is not a public API. The request carries immutable identities and declarative caps only; it has no command, module, URL, settings, credential, image-selection, or filesystem-path fields.

Compatibility is intentionally strict: unknown fields and execution modes fail closed. Version only when a real cross-process consumer requires a changed shape.
