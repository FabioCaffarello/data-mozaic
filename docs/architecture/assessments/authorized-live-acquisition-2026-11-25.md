# Authorized Live Acquisition — implementation assessment (2026-11-25)

## Branch and baseline

Work is on `feat/acquisition-live-foundation`, created from integrated `main`/`origin/main` at `b2c2e1dce55ccbcd98f3af279fe6f49584ec6829`. Gate A PR #20 was squash-integrated at that commit. This assessment is a subsequent draft and does not change historical snapshots or accept ADRs.

## Repository evidence

- The CapturePlaybook v2 schema and `collector-ir/v1` support bounded `http.fetch/v1`, JSON and HTML selection, restricted field extraction, finite fixture link pagination, `output.records/v1`, and `output.raw/v1`.
- `CAPABILITY_BY_OP` currently maps fetch/discovery/pagination to fixture-only capability names. Both compiler backends bind fixture evidence into the BotBuild.
- `scrapy-fixture-v1` records `execution_mode: fixture-only`; generated Scrapy code validates exact fixture URL, response bytes, digest, and size.
- The installed worker catalog accepts only `backend_id=scrapy-fixture`, `capability_profile_id=scrapy-fixture-v1`, and `execution_modes=[fixture-only]`.
- BC04 `ScrapyContainerExecutor` creates the worker with Docker `--network none`, read-only root, non-root user, dropped capabilities, no-new-privileges, and resource limits. It cannot currently admit a live build.
- Scout Squid is scoped to authoring. Its current ACL allows CONNECT to port 443 and source host names, denies non-CONNECT methods, private/local destinations, and cannot see encrypted HTTPS methods, paths, or bodies.
- There is no BC01 source registry, BC11 production policy authority, BotRelease approval registry, live runtime image, or live admission endpoint in the integrated baseline.
- ExecutionBinding remains fixture-specific. It binds source/build/fixture identity but has no live release/policy identity.

These facts make the fixture boundary strong and the live execution path absent. A generic `network` toggle would be an unsafe bypass, so this increment does not alter the fixture executor or catalog.

## Changes in this branch

- Added draft v1 public schemas for BC01 SourceRevision, BC11 AcquisitionPolicy, and BC15 BotRelease.
- Added a BC15 consumer-side binding validator that rejects non-eligible source/policy, unapproved release, source/policy/release/build mismatch, invalid document digests, insecure DNS/TLS policy, and URL origin/path outside the approved scope. This validates consistency only; it does not authenticate issuer documents or make an authorization decision.
- Added tests with independently supplied source, policy, build, and release documents.
- Added a draft BCB Focus source dossier with official documentation facts, unknowns, and a narrow human-review exploration proposal.
- Added Proposed ADR 025 describing the separate runtime and controlled egress requirements.

## Gate B completion status

This branch does **not** implement or claim an authorized live collector. No live HTTP/Scrapy execution has been run. No controlled egress gateway, Source Catalog authority, BC11 authorization service, approved release registry, live worker image, live result-bundle format, live importer, or live BC04 admission exists yet. No external source was contacted by Scout or Scrapy. The draft contracts and validator are groundwork, not operational authorization.

The missing gateway is a material safety dependency. The existing `network none` worker is safe for fixtures but cannot fetch. Attaching it to a normal bridge and setting proxy variables does not prevent direct egress. The next implementation must establish and exercise an isolated gateway/worker network boundary before enabling the live backend. HTTPS CONNECT alone can constrain origins but cannot enforce encrypted paths or methods; either the trusted generated client must be part of the trust boundary or the gateway must terminate and inspect TLS under an explicit trust model. No such choice is accepted in ADR 025.

## Focus dossier basis (accessed 2026-11-25)

- BCB Open Data Portal lists BCB/Dstat as maintainer, declares ODbL, describes aggregate expectations statistics derived from Sistema Expectativas de Mercado, and links JSON, Swagger, data browser, and OData resources.
- BCB documentation says Focus summarizes market projections (not BCB forecasts) and publishes weekly. The report's data reference/cutoff and public release time are distinct temporal values.
- The portal marks the institution-level JSON endpoint disabled due to confidentiality risk; the candidate case expressly excludes it.
- The Swagger page timed out through the browser reader and the OData endpoint returned XML, so no exact entity, field schema, parameter, quota, or source record was verified.

Source links are in [the proposed BCB Focus case](../../../examples/use-cases/bcb-focus/README.md). The dossier is documentation-derived, not captured source evidence.

## Operational review items not satisfied

- Keep all fixture profile tests unchanged and require them in CI.
- Design a network with no worker direct route; prove both authorized proxy/gateway access and direct bypass denial using the actual runtime.
- Implement release and policy authority or explicit trusted local-only adapters, with issuer authenticity and revocation semantics.
- Extend BC04 binding and durable run admission to immutable live release/policy references without weakening current leases/fencing/finalization.
- Implement a separate Scrapy live profile and response result contract; preserve bounded, closed bundle validation and sanitize response metadata.
- Integrate the trusted importer with BC06 custody, BC05 output contracts, and BC04 finalization; test all three real stores using only controlled test servers.
- Add failure injection, replay, stale attempt, redirect, DNS/IP, TLS, budget, deadline and oversized-body tests against the actual isolated live profile.
- Human review and separate authorization are required before any external source exploration or acquisition.

## Controlled implementation evidence (subsequent update)

The assessment above records the initial Gate B baseline and is retained as historical evidence. It is superseded for the controlled JSON vertical by the following execution on this branch; it does not accept ADR 025 or authorize an external source.

- Command: `make authorized-live-json-acquisition`.
- Controlled CaptureRun: `fa452622-36d9-4dd1-a08e-aaacd0a0227a`; attempt: `04ecb6cd-4a79-5cfb-8fac-e7e3bc15091d`, generation 1.
- Compiled live BotBuild: `eb3f57249fc670e8ff09099e8273dc8e175955f485d86c209763433b8491da3c`; manifest digest `sha256:8ba53183a64b64d7561fe3562e2dd2b3c26cc41d468f0d50764509f481b2f7c7`.
- Approved controlled-test release: `controlled-live-release-d39a70e58c4d`; runtime image digest `sha256:c070726505b4e1d936fddd1361279a0df453a3cfcf27d9a96ba5c308bcf04ba5`.
- Gateway fetch: `f849a58e992c24b1b96db1e03bbdc1ec`; two independently expected records passed the trusted result verifier.
- BC06 artifact: `artifact-96cb2a25a99fd4ffe02de8f2dd7eca157945cf434a08c1251bdb6d1f4901fe41`; retrieved bytes matched `sha256:59ed84aab12fcfd6f2fa6bed45d84a2ade7cc851c32669094bb41539a4447b2c`.
- BC05 OutputSet: `set-f04f24003478df2947538a2d0b583faa66e3366d3027702fea62c57fc077ddea`; manifest `sha256:3622ad899973c2e6ae11a3731e6af04c7f02dff23f932d4051897a6f807d33a2`.
- BC04 finalization reservation: `res-286cc38377e4afb1e525650ee178efefb1b5457ed75734c84e6084ace909470e`; BC05 publication `pub-96cc1e01d438c0c486418b0680ab7fdd1be807e1852ef70271bcbaf725da52a8`; durable BC04 state `succeeded`.
- Import replay returned the same output set and manifest. The integrated importer rejected stale-attempt evidence, cross-tenant evidence and a tampered worker evidence digest. Independent BC05 output read and BC06 artifact retrieval/digest verification passed. Gateway restart and concurrent quota behavior remain covered separately by Go tests; they were not restarted or saturated in this acceptance run.

This acceptance uses a generated, test-only signed authority snapshot and a TLS server reachable only over the controlled Compose network. It proves the delegated JSON path and local persistence integration, not production operator governance, HTML or live pagination. ADR 025 remains Proposed. No real source was accessed.

## Controlled HTML vertical (subsequent update)

- Command: `make authorized-live-html-acquisition` (exit 0).
- Run `35ac7942-2230-4a8f-914d-6c553f56eb6c`; attempt `d1468633-99c3-5158-aaab-86c889e2e72e`, generation 1.
- BotBuild `1830b4470aafb033c787d1facee77628b4770a2323c051ba865579d885c7ae9d`; manifest digest `sha256:95323cbe0e655844bb6410a6bda60dae6adb54bffc0548deae8648539545a059`; test release `controlled-live-release-df92eb9994a8`.
- Gateway fetch `1789fa3e72ad4c23a27ec69d267ceefc`; independent HTML record `html-1` was accepted and published.
- BC06 artifact `artifact-2463e525389754a057fa0d57b7d20d67e4999b002d1867dd9789725c815d2857`; retrieved digest `sha256:159a9d0aed4acd5deb5b4482aa9bc790a325016e7fc55c2aeb1e040441967f1d`.
- BC05 OutputSet `set-cc9a3392fe264ac7255668a55fb543b576d2992b515d20e559c090a2eed83506`; manifest `sha256:f5eb1795a3cc25d80838041e9e293fd534b5b9115a3f770c541718a686f25724`.
- BC04 reservation `res-e11e6836fb094fcd39b9168abce58291ce75a79c1e66a450ca6edbed2852a0b5`; publication `pub-d8917cf6c372545d3b04153c2d664a3c630c8d306615c1d6ec5e1b60f3855843`; state `succeeded`.
- The live backend now compiles the bounded HTML records selector and optional raw output node. The trusted importer checks the controlled HTML shape against independent expected records and gateway evidence.
- Finite pagination is not implemented. Redirect, oversized, slow and malformed controlled endpoints exist, but are not part of this durable acceptance path; their integrated live importer/finalization behavior remains unverified.

## Repeated controlled JSON acceptance after HTML generalization

- Command: `make authorized-live-json-acquisition` (exit 0).
- Run `e0d87878-2230-4a8f-914d-6c553f56eb6c`; attempt `710f85ff-353c-586e-a92c-e1e188a8942c`; generation 1.
- Build `449407f36a26af74076fe7bf67bdbb7056e161009dae6c8f14ee2a65adfd675d`; manifest digest `sha256:67e1ecdd6f8f4416a396d85a03489e27ca7b918f672f8e8f94fd7df3303f01f4`; release `controlled-live-release-ad416161ccad`.
- Fetch `0051304bf0d338f7ac38ccc7324f944a`; 2 records; artifact `artifact-9031d529a6deb35df82faea6f3b701ede6ea053a3aa67d393c4374e62587b16a`; retrieved bytes digest `sha256:59ed84aab12fcfd6f2fa6bed45d84a2ade7cc851c32669094bb41539a4447b2c`.
- OutputSet `set-70ccf5f2bbc6457f964fbf9b7810e8104322fd1ed656e616df880bf8a00f0759`; manifest `sha256:7b766addcc519020c4fafff787b4bafece6a438ad56349b121f8b29afde9d50c`; reservation `res-ae6203af2041a7d66857f05c20d57a995ddc9e68016042a64bad2507741cfd22`; publication `pub-d7302cdb795dd353ea28b93948299fb300fa9115a372a1a6ae29be3b997322e4`; BC04 state `succeeded`.
- The protected fixture-only E2E also passed after the live HTML changes: run `7a005da9-cc40-4bb4-a3a3-aa71050fb768`, 2 outputs, `network=none`, fresh-process replay and BC04-authorized publication.

## Bounded live pagination vertical (subsequent update)

- Command: `make authorized-live-pagination-acquisition` (final rerun exit 0).
- Run `421f39ee-e561-4c1f-ba09-f3fbc53def84`; attempt `627af9d1-184a-5406-a00d-6c05a5aeb51b`, generation 1.
- BotBuild `7b715afbd4420ca33fbc265a7edc305bd74f827a9b1c4539c91da3a528f0ad93`; manifest digest `sha256:39c29d31ff3f7ec7c14fd64dd5c31569116066f4b57f0fe45b4d91209e1493b9`; release `controlled-live-release-b5de5159e959`.
- Two gateway fetches in traversal order: `2887274ec8eee0d2efb6ed7adeda9261`, `e3c59c9a2fbdb820a4a9c7b365997dd2`. The trusted importer checked the expected page path sequence, exact run/attempt bindings, actual body bytes and digests, one next-link transition, natural termination, and record-to-fetch provenance.
- Two independent records were published. BC06 artifacts: `artifact-27dc47414934b8ddd3430eba95cb4800e0f98d8a3e3e5ff39eb9004542c988c5` (`sha256:31b3f3247dbd31bc1acd6775d247ccb6f5202e0c707747cbf2a51ff010c5a288`) and `artifact-56c4854aa9327195637eec515ade141f431b52290b55ac63609aff7e296c8174` (`sha256:3469b6b2e3299c5b217408f36341edeb1ad566f5a5b591b4bebb240420148b05`). Both were independently retrieved and verified.
- One BC05 OutputSet `set-a37d33826ca00bc3f248e9fb70c7faec2b74640e548243bac7a228ac1d72be91`; manifest `sha256:4670da9ab809bdb788088f9588bf75a778ccad6abad108a2881398efaed5f806`; reservation `res-1019b367dfa29bac81ff63635cafbe028491bddea536b83228fdc51af238aa84`; receipt publication `pub-6e8da049b2c7847277a18437cf16e37c1490bd36dff93b98a225ec1225222333`; BC04 state `succeeded`.
- Scrapy applies a conservative asynchronous 1.1-second page delay; gateway request-rate and run-level request/byte quotas remain authoritative. The test policy permits two requests; the compiler rejects pagination whose page limit exceeds playbook request/page budgets. Unexpected gateway budget/deadline failures fail the worker before import/publication.
- Negative stale-attempt, cross-tenant, unknown-fetch and divergent-worker-digest checks execute before import in the same acceptance command. The gateway container smoke additionally denies direct worker routing and unauthorized origin access. Gateway redirect, shared quota, concurrency and restart semantics have Go tests; those cases have not all been exercised as durable BC04-to-BC06/BC05 live runs.

## Final JSON and HTML regressions after bounded pagination changes

- `make authorized-live-json-acquisition` exited 0: run `e72bc5db-9b42-4b32-a0c5-32a629ac0046`, attempt `0220203f-5b12-53d0-a25d-3925678cb8ab`; build `3bc8e5877cfb3b8daea157d371d126a27061eccf35040edfa9c3d03f0641871c`; fetch `f52df11fd24b72e8ea6c0c2204b35515`; artifact `artifact-64abde3a6e43eb67f28b59c542632c0fc656d63397f5c43628e9c0b3757206c2`; OutputSet `set-d6cb4332e121f34baf705ef4b3b0f78a9b54deafd222a8ce20e2f3325fd870ba`; receipt publication `pub-6fa0aab3fcd4f4387471f916fc5c988ffa7568b531a6a530afe236964ff5b472`; state `succeeded`.
- `make authorized-live-html-acquisition` exited 0: run `ad294e0f-0260-45f0-82b2-c28f4eddd96a`, attempt `82d0d7cf-8c8a-5b68-b99c-22468d0cb2f0`; build `2b0eb844b60c6a333f0911fc672d8cee962e4ae67a5fd05722015f83d47fc19f`; fetch `5c7f40e49da356b3d2e4a6deb09f7301`; artifact `artifact-8c743737a4438a1b866f3aba3f56a7f31491dab9516ec5f23421095720380e1b`; OutputSet `set-2750b60df605c5527a0914cda0281ca764c3474d79af786ca8de365a68bd5332`; receipt publication `pub-8900a54a5c55380af69c78f69bf4340e7cba72d24af70f1c79cb6aedc4ec07`; state `succeeded`.

These are separate controlled acceptance executions; the paginated run above is independently identified and contains both of its own fetches, artifacts and records.
