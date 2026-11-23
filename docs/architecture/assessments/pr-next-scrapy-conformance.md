# Scrapy fixture backend and cross-engine conformance — implementation evidence

**Snapshot:** implementation branch `feat/collector-scrapy-fixture-backend`, based on remote `main` commit `a11d5142231e8b8ad5c9403732d9cb4aa209e29d`. This document records local evidence; it does not represent CI, PR approval, ADR approval, production authorization or merge.

## Scope and ownership

All implementation remains within BC15 at `apps/collector-engineering/collector_engineering/`. BC15 owns CapturePlaybook validation, `collector-ir/v1`, shared extraction/provenance, capability decisions, compiler outputs and BotBuild verification. `backend_python_http.py` and `backend_scrapy_fixture.py` are concrete backends; `reference_runtime.py` is the BC15 extraction/reference runtime; `scrapy_fixture_runtime.py` adapts Scrapy acquisition into verified BC15 documents and bounded output. No `libs/python` runtime, worker service, generic plugin framework, Playwright backend, production authorization or distributed scheduler was added.

## Runtime identity and evidence boundary

BotBuild includes the exact first-party runtime files used by its runner. The manifest identifies each file with SHA-256 and a deterministic aggregate digest; verification checks exact membership, content, backend/toolchain profile, dependency lock, and build ID. Generated runners prepend the build-local runtime path, require imports to resolve from that package, and verify the packaged identity before execution. This detects accidental or post-distribution byte drift; it is not a signature or authorization decision.

Filesystem/index/body evidence inspection is in the cohesive private `fixture_evidence.py` module. The v2 validator still owns schema, semantics, binding, scope, budgets and diagnostics. The v2 schema and canonical IR golden are unchanged. Provenance now explicitly contains source ID/revision, requested/effective URLs, evidence reference, raw digest, operation ID, page index and record index; the fixture backends have no redirects, so requested and effective URL currently match.

## Scrapy backend

`scrapy-fixture` emits a deterministic, operation-specific Spider from Typed IR only. It uses Scrapy Requests/Responses, Engine, Scheduler/dupefilter, Downloader, generated callbacks, closed settings, signals/stats and a bounded record Item Pipeline. The downloader maps only exact, in-scope HTTP/HTTPS fixture URLs to digest-checked responses. All other schemes and Scrapy network fallback handlers are disabled. Retry, redirect, cookies, extensions, standard middleware, feed output and arbitrary project settings are disabled. Output is sequence-keyed by page and record and atomically published only after a successful crawl.

The runtime shares BC15's existing HTML/JSON selection and conversion functions. No Parsel/lxml parser semantics are introduced. The capability profile is fixture-only and rejects browser rendered DOM before BotBuild creation.

## Cross-backend results

On the local macOS runner, `make collector-playbook-examples` verifies socket denial and runs the Python Reference examples against their independent expectations. `make collector-cross-backend-conformance` compiles and verifies distinct Python Reference and Scrapy BotBuilds from the same canonical IR for HTML simple, multipage catalog and JSON API examples; both outputs match their independently authored JSONL oracle byte-for-byte, including order and provenance, and common execution report counts/digests agree. Fixture tampering, missing fixture, and a failure after records have been buffered all return failure without a published output directory. The browser fixture is rejected by both capability checkers. macOS `sandbox-exec` is weaker than the Linux container gate and is not evidence of Linux sandbox equivalence.

A separate local Linux container run was also performed through Docker using locked Linux wheels. It compiled and verified both backends on Linux, ran all three positive examples against their independent oracles, checked an actual denied socket probe, and verified missing-fixture failure/no partial output under `--network none`, non-root UID, read-only root, memory/CPU/PID caps and a temporary filesystem. It had no bind mounts or Docker socket. This is useful Linux runtime evidence, but the mandatory repository Integration Security Gate itself remains unrun locally and must pass in CI.

Unit coverage exercises deterministic generation, closed Scrapy settings, runtime file modification/removal/addition and incompatible runtime identity. Repository-wide checks and the Linux Integration Security Gate are required before publication. This local report must be updated with exact completed command results before a Draft PR is proposed.

## Remaining limits

- The actual Integration Security Gate has not run in CI for this branch; it must prove the committed runner path with non-root Docker execution, restricted read-only mounts, `--network none`, resource bounds, socket/scheme denial and output failure behavior.
- No remote-origin network authorization, BC11 run authorization, BC06 custody, BotRelease, distributed worker protocol, BC05 dispatch or BC16 frontier is implemented.
- Scrapy parity is limited to the tested static fixture subset. It makes no claim about live HTTP, redirects, rendered DOM, browser subrequests or Playwright.
- The backend has no runtime restart/checkpoint because this initial single-process fixture runtime has no distributed consumer requirement.

## Decision status

See [ADR 019](../decisions/019-scrapy-fixture-backend-and-runtime-identity.md), which remains Proposed. Publication readiness stays NO-GO until the complete suite passes and actual Linux Integration Security Gate evidence is available.
