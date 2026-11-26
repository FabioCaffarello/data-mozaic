# Immutable contract resolution — M3 implementation assessment

**Branch:** `feat/immutable-contract-resolution`
**Base:** M2 integrated main `9f5b5cc2ba12941c1f174f8cc1f05ee707d87dca`
**State:** M3 implementation on its feature branch, not yet integrated; ADR 028 remains Proposed.

## Observed baseline

- BC05 `PayloadContractRef` in `apps/acquisition-output/src/acquisition_output/domain.py` already identifies `contract_id`, `version`, and a `sha256:` digest. CaptureOutput and OutputSet preserve that reference.
- M2 `ExactJSONSchemaPayloadContractValidator` in `apps/acquisition-application/src/acquisition_application/application.py` hashed raw schema bytes but received a caller-selected schema path; the trusted execution context did not own the contract reference.
- BC04 `LiveExecutionBinding` persisted source, policy, release, build, runtime and authority snapshot identity in JSONB but omitted payload contract. Fixture `ExecutionBinding` likewise omitted it.
- BC15 `contracts/bot-release/draft/v1/BotRelease.schema.json` did not bind a payload contract. The signed live authority verified exact release and build bindings but had no payload contract field to retain.
- `docs/architecture/domain.md` classified BC08 Contract & Quality as future supporting. This increment establishes only the initial raw payload contract identity/resolution capability; dataset quality remains future.

## Selected implementation

BC08 owns `ContractReference` and the exact local immutable resolver in
`libs/python/contract-resolution`. It reads `contracts/payload/draft/v1`, a
code-reviewed installation manifest plus immutable schema documents. The
content digest is SHA-256 of exact UTF-8 file bytes, preserving the M2 raw-byte
convention. The registry has no network or mutation API and is not persisted
in PostgreSQL. Restart behavior follows the immutable application/repository
artifact; installation changes require a reviewed release.

The resolver accepts JSON Schema Draft 2020-12 only, enforces a 256 KB registry
and 64 KB per schema, rejects duplicate JSON members/non-finite values, unsafe
paths/symlinks, content mismatch, invalid schemas, and any `$ref` or
`$dynamicRef`. The current documents contain neither, so no dependency graph
is silently mutable.
Validation errors do not include payload instance values.

BotRelease v2 adds the exact payload contract reference while v1 remains
unchanged. New trusted live admission requires v2. BC04 persists the release
reference in the durable run binding; fixture execution binding carries the
same triple. `TrustedExecutionContext` derives it only from BC04, and the
importer rejects a caller-supplied output reference that differs. The BC08
resolver validates the payload through the existing BC05 payload-validation
port. BC05 continues to persist contract refs on outputs and OutputSets.

Historical unbound M2 bindings are not assigned a guessed contract and cannot
be newly imported under M3. Existing published outputs keep their existing
read path and stored provenance. No wire version or result bundle version
changed; BotRelease is explicitly versioned from v1 to v2 for the new required
binding.

## Library decision

`mozaic-contract-resolution` is a justified BC08-owned L2 internal package: resolution
is separate from acquisition orchestration, exposes exact reference/value and
schema validation behavior, and is consumed by the trusted acquisition
composition through the BC05 validation port. It owns no approval, release,
source, persistence, or execution state. It is not a wrapper around a JSON
Schema driver alone; the reusable responsibility is exact immutable
installation resolution, byte digest verification, fail-closed format/ref
policy, and safe diagnostics. Independent tests cover exact lookup, digest
divergence, schema content mutation, unsupported `$ref` and `$dynamicRef`, conflicting
revision entries, and payload validation. Full consumer conformance and any new
BC08 consumers remain future requirements.

## Validation evidence

Controlled acceptance ran against the persistent local PostgreSQL, MongoDB,
RustFS, gateway, and protected Scrapy infrastructure. No external source was
contacted.

- `make check`: passed after the `$dynamicRef` rejection test was added.
  Root suite: 220 passed, 26 skipped for documented environment/profile
  requirements. `make test-libraries` includes eight
  resolver tests; BC04/BC05/BC06 Go gates, dependency checks, docs (78 Markdown
  files), naming, formatting and architecture checks passed. The
  acquisition-application suite has seven passing tests after adding the
  caller-contract substitution rejection case.
- `make execution-coordination-durable-tests`: 13 PostgreSQL integration tests
  passed, including live-binding round-trip, signed admission, current-attempt
  capability and finalization/reconciliation cases.
- Final `make execution-coordination-fixture-acquisition`: passed through Scrapy
  network-none execution, BC06 custody, BC05 publication, BC04 terminal success,
  and fresh-process replay. Run `6c1b83b2-1846-4d52-afbb-de9471b173b5`,
  OutputSet `set-96726646a01baf829c626313a88bfc19e93b4a51a97ce63f04da34650eb80386`,
  manifest `sha256:75a906d58c61169bf2e7347a1ee7536823372a52f3e9256ebc98824b0652a433`,
  contract `synthetic.html-simple-catalog.record` / `1.0.0` /
  `sha256:0ec5fd30c626bcadeb930aa729f121a6d479ce4b59ec39e6ad519f090fa1669f`.
- Final controlled live JSON passed with run
  `2c195480-12d4-496d-99b1-e5d17c59c0b7`, release
  `controlled-live-release-1e5678ba4b1e`, OutputSet
  `set-83dc6175838569024a23657f6990f12c0dafc9614cf2f631412ecc8a5c2fd0e3`, publication
  `pub-ddea91051ae0facfaae345083c4673101533c9bc11712bcbc0d56ff0fb1a61d0`, and
  contract `controlled-live-json.record` / `1.0.0` /
  `sha256:dd8976c347bf0daac2f84d42429fdd8059bf11e810dbe0468162c6538bb4e6c`.
  It reached BC04 success with an exact BC05 receipt and independently checked
  custody bytes; the command reported contract identity/version/digest.
- Final controlled live HTML passed with run
  `4e48e2f3-d2c4-47ce-9a3b-90877cff3c48`, release
  `controlled-live-release-f5bf047d433b`, OutputSet
  `set-a90e6cb2ad87d037114b1b74da6a8601a7dfca727c4e74d3c72f7375dd8a5e2e`, and
  contract `controlled-live-html.record` / `1.0.0` /
  `sha256:ba3fd2ff0d6fe046ba7ad5048adac530fe1c7c9f6fcb2adcd48860681b98b116`.
  Earlier natural/planned pagination targets passed. The authorized redirect
  passed with one redirect; denied redirect failed closed
  with no publication receipt. Final authorized redirect evidence: release
  `controlled-live-release-4b35948f7001`, run
  `ebdcda6a-dc2e-4797-86b7-353246a04c75`, OutputSet
  `set-47f5296498a69272f13b7aa407cd7b802028c28048c96d5928a8fabb3ef8cd6e`,
  publication `pub-c935b9c97d95451f81152e54860484ccaba20eda1234570d4cd0e577d2140855`,
  contract `controlled-live-json.record` / `1.0.0` /
  `sha256:dd8976c347bf0daac2f84d42429fdd8059bf11e810dbe0468162c6538bb4e6c`,
  terminal `succeeded`.
- Resolver tests cover exact lookup, unknown ID, digest mismatch, installed-byte
  mutation, conflicting same identity/version entries, historical resolution
  alongside a newer version, invalid schema, duplicate JSON keys, external
  `$ref` and `$dynamicRef`, and payload validation. App tests reject caller contract
  substitution before side effects. BC04 authority tests reject BotRelease v1
  for new M3 admission.
- Final `make check`, `make test-libraries`, and `git diff --check` passed after
  the resolver change. Fixture plus controlled live JSON and HTML acquisition
  were rerun against this tree and passed.

## Limits and next increment

This local registry is not an approval or administrative workflow; repository
review plus BC15 authority release is its installation boundary. The schema
format is limited to self-contained Draft 2020-12. Compatibility is exact
identity/version/digest, not inferred semantic compatibility. Source/Release
operational lifecycle, schema eligibility/deprecation, dependency graphs,
BC13 input contracts, and durable registration remain M4/future work.
