# mozaic-observability

Status: experimental internal technical library, consumed by `mozaic-scout`. Distribution name: `mozaic-observability`; import: `mozaic_observability`.

Provides standard-library logging helpers for allowlisted structured event fields and context-local correlation IDs. It never configures the root logger, installs handlers, or emits at import time. Applications own logger configuration and destinations.

It excludes domain policy, authorization, secret scanning of arbitrary free-form messages, exporters, metrics/tracing SDKs, and BC14 models. Callers must use static event names and approved identifier fields; arbitrary message text may still contain sensitive content. The API is experimental `0.x`; no external compatibility promise is made. Upgrade by pinning the workspace distribution and running its tests plus Scout consumer tests.

Run independent tests with `uv run --locked --package mozaic-observability python -m unittest discover -s libs/python/observability/tests -v`.
