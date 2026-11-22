# PR #10 — Playbook v2 and Python Bot Compiler implementation evidence

Status: implementation evidence for the isolated PR #10 branch. CapturePlaybook draft/v2 and `collector-ir/v1` remain draft contracts. ADR 018 remains Proposed; this assessment does not approve release or production execution.

## Vertical capability

BC15 now parses a closed YAML CapturePlaybook draft/v2, validates it against the versioned JSON Schema and fixture evidence, creates a canonical Typed IR, checks a versioned capability profile, generates operation-specific Python, and verifies a local BotBuild manifest. The Python HTTP Reference runtime reads only local fixtures, verifies their digests, and writes bounded JSONL, raw references, provenance, and a separate execution report.

BC14 remains owner of Scout observations, findings, transcript order, and evidence references. BC06 remains owner of raw artifact custody; this increment carries fixture digests/references only. BC11 authorization is not inferred from successful compilation. There is no live HTTP client or production runtime in the Python HTTP Reference backend.

## Milestones

| Milestone | Implementation and consumer | Verification evidence | Limitations |
|---|---|---|---|
| M1 — Contract & Semantic Validation | Draft/v2 schema and BC15 loader/validator; consumed by CLI `validate` and compiler; v1 entry points remain unchanged. | Schema/parser/v1 regression and semantic negatives in `tests/test_collector_playbook_v2.py`; fixture-backed validation in all executable examples. | Draft/v2 is not a published or release-approved public contract. Fixture match/conversion checks cannot prove business correctness on a live source. |
| M2 — Typed IR & Authoring Tools | `collector-ir/v1`, stable canonical JSON, selector/fixture inspection tools; consumed by Python backend and Codex authoring workflow. | Golden IR + SHA-256, deterministic generation tests, bounded `inspect-fixture` / `try-selector`, invalid mapping diagnostics, authored fixture demonstration. | Inspector exposes bounded samples, not full documents; selector testing proves only behavior against those fixture bytes. No Scout structured DOM inspection API was added. |
| M3 — Python Backend & Offline Runtime | Versioned capability profile, generated operation-specific Python, fixture-only helpers, BotBuild manifest and verifier; consumed by `compile`, `verify-build`, `run-example`. | Determinism, source compilation, capability rejection before build publication, manifest tamper/path tests, output budgets, offline run under OS isolation. | No Scrapy/Playwright backend, no real source fetch, no signing, no runtime/release governance. Local macOS sandbox provides CPU/time limits and network denial, but no memory limit equivalent to Docker. |
| M4 — Executable Examples & Acceptance | Synthetic HTML, bounded multipage HTML, JSON API, browser-capability negative, and Codex fixture-authorship example; CI invokes example gate. | `make collector-playbook-examples`: network probe denied; three positive examples and authorship example independently compare JSONL; negative example validates and fails only at capability check. | Acceptance is synthetic/offline. It is not operational acceptance for a remote source and does not establish PR #9 Scout deployment readiness. |

## Contract and operator subset

The checked-in v2 schema is JSON Schema 2020-12, draft-only, and copied byte-for-byte into the BC15 package. YAML uses a safe loader, rejects duplicate keys, aliases/anchors, over-1 MB playbooks, unknown fields, and arbitrary tags. v1 remains selected explicitly through the existing v1 validator; there is no migration or silent reinterpretation.

Versioned operations are `http.fetch/v1`, `links.discover/v1`, `pagination.bounded/v1`, `select.html_records/v1`, `select.json_records/v1`, `extract.fields/v1`, `output.records/v1`, and `output.raw/v1`. The contract also recognizes `browser.render_dom/v1` and `select.rendered_dom_records/v1` for explicit unsupported-capability diagnosis; no browser backend is implemented. Pagination is finite and consumes discovered same-scope links in fixture order.

Budgets include requests, pages, response bytes, records, and output bytes. Selectors and restricted JSON pointers are tested against digest-verified fixture evidence. Mapping validation checks selected fixture records, required/optional values, scalar conversion, cardinality, scope, bindings, DAG reachability, and capabilities. Decimal outputs use canonical base-10 strings. Missing or malformed required data fails closed.

## Backend and build identity

The Python HTTP Reference profile supports `http.fetch.fixture`, `html.css_select`, `json.path_restricted`, `links.discover.fixture`, `pagination.bounded.fixture`, `records.extract`, `output.records.jsonl`, and `output.raw.reference`. All fetches resolve exact URLs from the local fixture index. A request for `browser.rendered_dom` fails before publishing a build; there is no fallback.

Generated source binds each IR operation to a concrete Python step and uses the versioned BC15 reference runtime. The manifest records normalized playbook and IR digests, backend/profile/compiler versions, Python/platform/runtime identity, dependency lock digest, generated file digests, and fixture URL/evidence/digest identities. Build identity excludes timestamps and development output paths. `verify-build` rejects incompatible versions, missing/extra/tampered files, path traversal, and symlinks. Digests provide integrity comparison, not a signature.

## Examples and independent oracles

`examples/playbooks/html-simple/`, `multipage-catalog/`, and `json-api/` contain independent expected JSONL records. The catalog oracle includes page ordering and provenance for pages 1 and 2; its third observed link is not visited because `max_pages` is 2. `browser-capability-unsupported/` has no records because compilation must reject it; its oracle is the expected capability diagnostic. `codex-authorship-demo/` documents a new synthetic fixture authored from an objective, a bounded inspection, selector experiments, an intentionally failing candidate, correction from `SELECTOR_NO_MATCHES`, compilation, verification, offline run, and independent comparison. No remote source or Scout browser was used.

## Commands and results

Required fast gate: `make ci` (which runs `make sync` and `make check`). It passed on the implementation worktree. The latest review run reported 194 tests, 26 skipped. Skips were existing opt-in PostgreSQL, Docker E2E, and protected replay cases whose environment gates were not configured; they are not counted as executed.

`make check` passed after the locked dependencies were synced, including lint, format, docs, naming, architecture, and unit tests.

`make collector-playbook-examples` passed on macOS. Its OS sandbox socket probe was denied by `sandbox-exec`; each positive generated bot ran with network denied and matched its independent oracle. The Linux CI Integration Security Gate runs the same target using a non-root Docker container with `--network none`, read-only root, memory/CPU/PID limits, and restricted mounts. The first published run exposed an editable-package import issue in the container; after mounting the versioned runtime source read-only, the Linux run `45063064428` passed the sandbox probe, all four positive/negative examples, and the complete Integration Security Gate on commit `c3d73d2`.

## Capability matrix and deferred work

| Capability | Python HTTP Reference in PR #10 | Scrapy follow-up | Playwright follow-up |
|---|---|---|---|
| Fixture fetch and digest verification | Implemented | Backend-specific compiler/runtime required | Backend-specific compiler/runtime required |
| HTML CSS selection and typed fields | Implemented with selectolax Lexbor | Not implemented | Rendered-DOM semantics not implemented |
| Restricted JSON paths | Implemented | Not implemented | Not applicable to rendered-DOM work |
| Observed-link bounded pagination | Implemented against fixtures | Not implemented | Not implemented |
| Live HTTP / browser execution | Unsupported | Deferred | Deferred |
| Browser rendered DOM | Explicit capability rejection | Unsupported | Deferred with Scout authorization/deployment gates |

## Limitations and publication readiness

- P0: none identified in the implemented synthetic offline path; production execution remains intentionally unavailable.
- P1: none remain after the Linux runtime import fix. macOS local execution still has no equivalent memory cap; Linux Docker isolation is the required CI evidence.
- P2: evidence fixtures and selector match counts cannot establish semantic correctness. Independent expected records and human review remain necessary.
- P2: build digests are not signatures and do not constitute authorization or release approval.
- P3: no field normalization beyond trim/collapse-whitespace and strict scalar conversion; no date/locale inference, browser, authentication, live HTTP, Scrapy, Playwright, or release governance.

The branch is ready for review as a single Draft PR after Linux CI's required Integration Security Gate passes and reviewers accept the Proposed ADR/contract decisions. The PR has not been published or merged by this implementation session.
