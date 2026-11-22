# PR #10 — Independent compiler review evidence

Review base: `b483ad680b98b4fa22c1522f6a7d4a9d08243b76`  
Reviewed implementation: `70008c90b43480c0977d9313b8d1c46e6bcd580a` plus the audit corrections in this worktree.  
Status: Draft PR review evidence only. CapturePlaybook draft/v2, `collector-ir/v1`, and ADR 018 remain Proposed/draft; this is not release approval.

## Findings and corrections

The review found several confirmed issues and corrected them on the PR branch:

- Malformed non-string IDs could raise `TypeError` during duplicate detection. Schema diagnostics now safely leave malformed values to the schema validator.
- A playbook fetch could compile without an exact fixture URL/evidence binding, and a matching evidence ref on another page could mask the wrong fixture digest. Validation now checks the exact URL, evidence ref and digest, content type, status, scope, and observed pagination target fixtures.
- The offline runtime trusted a mutable fixture index after compilation. Generated bots now embed the compiled fixture identities and verify the source/revision, URL, evidence, digest, byte count, content type, and status before fetching.
- Pagination could revisit its seed or a duplicate link. Runtime pagination now maintains a visited set and preserves first-seen order.
- Restricted JSON array indices accepted noncanonical forms. Only canonical nonnegative integer indices are accepted; duplicate JSON object keys are rejected in fixture indexes and JSON extraction.
- Multiple record sinks could target the same fixed output path. Semantic validation now permits exactly one record sink and at most one raw-reference sink.
- Selector experimentation returned zero matches without a stable diagnostic. It now reports `SELECTOR_NO_MATCHES` while distinguishing valid CSS syntax from semantic correctness.
- Fixture-backed validation now rejects a response or bounded pagination fixture set that exceeds the declared response-byte budget.

The `collector_engineering.playbook_v2` module is large and keeps parsing, JSON Schema validation, BC15 semantics, fixture-backed evidence checks, and IR construction together. These responsibilities form one versioned BC15 pipeline, and generation/runtime remain separate. No broad refactor was made during this review. Before adding more operators, the smallest useful extraction would move fixture-backed verification and selector/path coverage into one BC15 fixture-validation module with the current validator as its caller; keep semantic typing and IR construction together. This remains a P2 maintainability concern, not a release blocker for the closed v2 subset.

## Compatibility and ownership

The v1 schema digest is unchanged at `784d9bae0c262d9e1b21d19e55543fc7c2f0ca6795507e1d94248dbd611f5a80`. Existing v1 draft, validation, raw replay, and independent acceptance tests passed. The v1 schema and behavior are selected explicitly; no v1-to-v2 migration or new network path was added. A small shared YAML loader primitive preserves duplicate-key rejection for both versions.

BC14 remains owner of observations, findings, transcript order, and evidence references. BC15 owns playbook semantics, IR, compilation, capability checks, and BotBuild. BC06 raw identity is carried as evidence refs/digests; raw custody is not reimplemented. Compilation does not create BC11 authorization. The Python backend has a concrete fixture-only implementation; no Scrapy, Playwright, Docker, or model dependency was added to BC15 semantics.

## Build, extraction, and isolation audit

Generated Python is operation-specific, uses `repr`-encoded values and the versioned reference runtime, and does not evaluate DSL text. Different HTML and JSON playbooks produce different Python; source compiles. A crafted selector containing a newline and Python-looking text remains a string literal. The browser example passes schema, semantics, and IR construction and fails capability checking with `CAPABILITY_UNSUPPORTED` before creating a build.

The build manifest records normalized playbook/IR digests, compiler/backend/profile/runtime/toolchain identity, lock digest, generated files, and fixture identities. Build identity excludes timestamps and development paths. The verifier rejects incompatible versions and file-set/digest/symlink/path mismatches. Digests are not signatures or authorization.

The macOS runner's `sandbox-exec` policy denied the external socket probe and examples ran successfully. It applies CPU and wall-clock limits but does not provide the Linux container's memory/PID limit or equivalent filesystem confinement; it must not be described as equivalent isolation. In Linux CI, the Docker path uses non-root execution, `--network none`, read-only root, bounded CPU/memory/PIDs/time, read-only package/build/fixture mounts, and an output mount; the container receives no Docker socket. The Linux path remains to be observed in the PR checks. The `unshare --net` fallback, when Docker is absent, denies networking but does not provide equivalent memory or filesystem controls.

The first published Linux run exposed a packaging defect before the HTML example could execute: `uv sync` installed this monorepo package editably, while the container mounted only `site-packages`, so `/build/bot.py` could not import `collector_engineering`. The Docker runner now mounts the versioned BC15 runtime source read-only and includes it on `PYTHONPATH`; a unit test checks that mount construction. A new Linux run is required to verify the complete fix.

## Additional local authoring exercise

A new synthetic offer-page fixture was authored from an objective and inspected only through `inspect-fixture` and `try-selector`. The first selector `.offer-card-wrong` returned `SELECTOR_NO_MATCHES`; `.offer-card` returned two bounded samples with evidence digest `7c0a0ad80603ce714eff2aa4858ee6dcb4a29c787624491f13797da3fa7e0f83`. The playbook was then written, validated, compiled, verified, run through `run-example`'s network-isolated runner, and compared against a separately authored JSONL oracle: validation passed, four generated files verified, and two records matched.

The oracle was not read by the CLI or used by validation/compilation; comparison occurred after offline execution. This was an agent-driven local workflow, not an independent human blind study: the same development agent created the synthetic fixture and its oracle, so the result demonstrates usability and deterministic acceptance but not evaluator blinding. No network source or Scout approval was used.

## Review disposition

No remaining P0/P1 finding was identified in the local synthetic offline path after the corrections above. P2 limits are the validator module's size, the non-equivalent macOS resource isolation, and the synthetic/non-blind nature of local authorship evidence. Existing opt-in database and Docker/Scout E2E checks remain distinct from executed local tests. Draft publication is acceptable only after final local gates, push, and a real passing Linux Quality Gate and Integration Security Gate. No merge is authorized by this assessment.
