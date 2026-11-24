# ADR 020 — Scrapy Collector multi-bot project

- Status: Proposed
- Date: 2026-11-23
- Owners: BC15 BotBuild contract; Scrapy Collector worker
- Scope: assembly-time BotBuild catalog and isolated fixture-only Scrapy execution

## Context

PR #12 added BC15's fixture-only Scrapy compiler. It produces one standalone `bot.py` per BotBuild with a `GeneratedSpider`, an embedded `mozaic_bot_runtime`, runtime identity verification, and a `CrawlerProcess` bootstrap. The monorepo does not yet contain an installable Scrapy worker or a catalog. The standalone verifier depends on the compiler checkout and root `uv.lock`, so the current verification implementation cannot be installed directly in a worker image.

The product requirement is one Scrapy project hosting N compiled bots, resolving an exact logical bot/build pair, and isolating each execution. The worker must not parse Playbooks or compile IR. CapturePlaybook, the IR, semantic extraction/provenance, capability policy and BotBuild production remain BC15-owned. The first worker profile is fixture-only and has no network fallback.

## Decision proposed

1. Create one installable `apps/workers/scrapy-collector` project. Assembly copies verified BotBuilds and their digest-checked fixture evidence into an immutable image and writes a canonical static JSON Bot Catalog.
2. Keep a closed YAML assembly declaration containing only `bot_id`, `build_id`, expected BotBuild manifest digest, and include/disabled state. Artifacts are resolved below an administrator-controlled root by build ID; no code, module path, callback, output path, credential, or command is accepted from YAML.
3. Derive `bot_id` deterministically from `(source_id, playbook_id)`; source revision distinguishes build versions but does not change the logical bot identity. The worker requires exact `(bot_id, build_id)` selection; it never resolves `latest`.
4. Evolve the BotBuild manifest additively to `bot-build-manifest/v2`, carrying Playbook ID, logical bot ID, source identity, IR digest, profile identity, dependency identity, runtime identity, compiler host toolchain, explicit target platform, generated files, and worker compatibility. The optional original Playbook byte digest is passed by the compile caller or is null; it is never reconstructed from IR. Standalone consumers continue to accept manifest v1 explicitly; the worker requires v2.
5. Add the internal stdlib-only `mozaic-bot-build` library for structural identity and integrity verification. BC15 and the worker use the same implementation. Backend/profile/engine compatibility and execution authorization remain caller policy, not verifier behavior. Assembly additionally requires Python 3.12, Scrapy 2.20.0, the matching fixture runtime, the locked dependency closure, and an exact assembly target (`linux/amd64` initially); the launcher checks the catalog target against its actual platform before import.
6. Verify the complete build and expected manifest digest before importing generated code. The parent launcher never imports a BotBuild. A fresh child process loads exactly one build, so fixed `mozaic_bot_runtime` module names and the constant Scrapy spider name cannot collide across executions. The child environment omits inherited `PYTHONPATH`/`PYTHONHOME` and uses only the build's runtime directory plus the installed worker environment.
7. Use Scrapy's existing `CrawlerProcess` bootstrap and Engine/Scheduler/Downloader/pipeline from the BotBuild runtime. Add the worker's project settings as lower-priority defaults; the BotBuild retains its closed fixture settings. Do not add a custom scheduler, plugin framework, dynamic registry, or second implementation of Scrapy.
8. Run each execution as a fresh process; deployment uses a Linux container with non-root UID, read-only root, `--network none`, restricted read-only BotBuild/fixture content, writable execution output only, and CPU/memory/PID/deadline limits. Subprocess isolation is not treated as an OS sandbox.
9. Publish records/report only after the child exits successfully, the report states `passed`, and the records digest matches the report. Stage paths are execution-specific; failures, timeout, cancellation and crash remove partial output.
10. Keep Bot Catalog local to the worker image. Bot Registry, BotRelease, BC04 execution coordination, BC11 authorization, BC16 distributed frontier/rate gate and BC06 custody remain future context-owned capabilities.

## Alternatives

### Multiple spiders in one long-lived process

Scrapy supports multiple crawlers in one process, but reactor/logging settings and Python module state are shared, and the current BotBuild runtime package name is fixed. Rejected as the initial isolation boundary.

### Install generated BotBuilds into a live worker

Would allow per-bot updates without image rebuilds, but introduces mutable executable code, import/dependency collisions, non-atomic rollback and a service-side authorization boundary. Rejected.

### New generated-Spider ABI or namespace every runtime by build

Could centralize Scrapy components and avoid duplicated runtime modules, but requires changing current generated source and defining a cross-context ABI before a second runtime consumer is operationally proven. Deferred; one BotBuild per child process resolves the collision without changing the Spider format.

### Duplicate the verifier in the worker

Rejected because BC15 and worker could disagree about canonicalization, file-set checks and symlink handling. The narrow shared library has two real consumers and no runtime dependencies.

### Scrapyd

Scrapyd provides project versioning, job scheduling/status/cancellation and process launch. It does not provide BotBuild trust/authorization, fixture isolation, global rate coordination or BC04 lifecycle semantics. Deferred until its job API and operational features are required; the first worker uses a small local launcher.

## Consequences and trade-offs

- A catalog update creates a new immutable assembly/image; image rebuild cost is accepted for deterministic deployment and rollback. BotBuild metadata records the compiler host and explicit execution target separately, so a macOS build is not implicitly considered a Linux artifact.
- Multiple builds of one logical bot coexist in separate build-ID directories. Removal/disable affects a new catalog; historical BotBuild bytes remain immutable.
- The project shares settings, execution lifecycle, diagnostics, and Scrapy process management. BotBuild-specific fixture downloader, pipeline, extraction and provenance remain in the verified BC15 runtime. This avoids copying semantics into the worker.
- The worker does not load all generated modules through default `SPIDER_MODULES` discovery. Catalog listing is metadata-only; execution imports only the selected module in a clean child process.
- The Scrapy version and worker contract are pinned. Existing BotBuild code that inspects private Scrapy runtime structures remains sensitive to that pin and must be covered by Linux conformance tests.
- Digests verify bytes relative to a known manifest; they do not prove publisher identity, authorization or release approval. A future signing/attestation design is separate.
- BotBuild v1 remains a supported standalone artifact. No silent conversion or worker execution of v1 is allowed.

## Security boundaries

Assembly runs in a trusted build pipeline and admits only verified BotBuilds and fixture bytes. Runtime never compiles or installs code. The image/catalog digest is anchored externally by deployment; the catalog's own digest is not a signature. No productive network or credentials are present. Linux CI must execute the actual assembled image under network/resource/filesystem restrictions.

## Validation criteria

- Preserve existing v1, v2 Playbook, IR golden, reference/Scrapy standalone and cross-backend tests.
- Verify v1/v2 BotBuild behavior explicitly; worker accepts v2 only.
- Exercise one HTML bot, one JSON bot and a second source revision of the same logical bot in one catalog.
- Reject bad IDs, disabled entries, duplicate builds, profile/runtime incompatibility, missing/extra/modified files, symlinks, path traversal, malformed/duplicate YAML keys and fixture drift before import.
- Run selected builds in separate child processes and compare records/provenance to independent example oracles.
- Prove failed/timeout/cancelled executions publish no result and do not affect the next bot.
- Run the installed image on Linux as non-root with read-only root, no network, restricted mounts and resource limits; no compiler checkout is available inside the image.
- Keep this ADR Proposed until human approval; implementation and green CI do not accept it automatically.
