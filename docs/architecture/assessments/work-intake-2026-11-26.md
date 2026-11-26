# M5 Work Intake — implementation and acceptance evidence

Date: 2026-11-26. Feature branch: `feat/work-intake-foundation`. This document
records implementation and local acceptance evidence for M5 review. Remote PR
and integration state are tracked by GitHub. ADR 030 remains Proposed.

## Operational composition and ownership

`work_intake.adapters.composition.compose_work_intake` builds the reusable
in-process BC13 facade over the BC13-owned PostgreSQL store, the existing
immutable contract resolver, the M4 lifecycle release resolver, and BC04's
public admission, outcome and cancellation APIs. It exposes submission,
bounded dispatch/reconciliation, submission inspection, authoritative
execution inspection and cancellation. The controlled acceptance invokes this
composition; dispatch or terminal lifecycle logic is not implemented only in
the harness.

The dependency direction remains BC13 → consumer-owned BC04/M4/BC08 contracts.
BC13 owns logical Input, immutable Submission intent and Dispatch intent. BC04
owns CaptureRun, CaptureAttempt, fencing, admission and terminal outcome. BC08
owns immutable Input and payload contract semantics. BC15 owns compiled
Playbook/BotBuild and runtime semantics. M4 resolves authorized revisions and
release eligibility. The protected worker executes; the Go gateway authorizes
operations and supplies authoritative FetchEvidence; the Trusted Acquisition
Application imports and verifies; BC06 owns artifact custody; BC05 owns
OutputSet and publication. BC13 does not read private BC04 tables or mirror
execution terminal state.

After admission, an operator uses the existing BC04 `acquire_attempt` and
`issue_execution_capability` interfaces, runs the protected worker with the
verified release build and capability, then uses
`TrustedAcquisitionApplication.verify_live_result` and
`import_verified_result`. The importer follows the existing BC06 custody,
BC04 finalization reservation and BC05 publication flow. This is not a worker
fleet or a new service. The API examples and operational boundary are documented
in [`apps/work-intake/README.md`](../../../apps/work-intake/README.md).

## Parameter binding and security

The v3 profile is deliberately one declared scalar query parameter on one GET
response. It does not support parameterized pagination, continuation, redirects
or additional requests. The compiler rejects pagination/continuation for this
profile; BC04 rejects a second gateway operation for a parameter-bound run.
Existing nonparameterized acquisition profiles keep their pagination and
redirect behavior.

The security checks remain separate:

- Source and AcquisitionPolicy constrain the broad authorized origin, method,
  path and allowable query values.
- The approved BotRelease v3 binds the exact immutable Input Contract
  reference, Playbook revision, BotBuild, runtime and payload contract.
- BC13 validates the submitted object against that exact Input Contract,
  persists canonical JSON bytes and a SHA-256 digest, and binds those values to
  the Submission and stable Dispatch operation.
- BC04 copies the exact values and digest into durable admission, the current
  attempt and the signed execution capability. It derives the expected initial
  target from the verified build and parameter binding.
- The worker checks the protected parameter file against the capability digest
  and rejects a substituted value, including another value allowed by the
  broader source policy. It constructs the query using component encoding.
- The gateway independently checks the initial target against BC04. It cannot
  authorize a later request for a parameterized single-response run.
- Trusted result import resolves the current BC04 binding, reconstructs the
  only authorized parameterized target from canonical JSON, verifies trusted
  FetchEvidence against that target, then resolves and applies the bound output
  payload contract.

Parameter documents are closed objects and bounded to 64,000 bytes. Strict JSON
parsing rejects duplicate properties and non-finite values. The profile accepts
only its one declared string or integer scalar; strings are bounded to 128
characters and URL encoded. Repeated or injected query parameters, extra
parameter keys, target changes and digest changes are rejected. Structural
Input Contract validation does not broaden network authorization. No policy,
origin, method, runtime identity, build or output contract can be selected by
the worker.

## Version compatibility

| Concern | Historical behavior retained | Parameterized behavior |
| --- | --- | --- |
| Playbook | v2 keeps its existing nonparameterized semantics | v3 declares the bounded query binding; unsupported DSL versions fail explicitly |
| BotBuild | manifest v1/v2 remains verifiable without reinterpretation | v3 requires and verifies the v3 input-query identity |
| BotRelease | v2 remains usable without an Input Contract binding | v3 requires the exact Input Contract reference |
| Runtime identity | supported historical identities remain `collector-runtime/1.0.0` and `/1.1.0` where their existing build profile permits them | v3 compiler emits the new `/1.2.0` identity; incompatible build/runtime bindings fail verification |

No historical artifact is rewritten and existing releases do not gain an Input
Contract retroactively. Focused BotBuild/runtime and contract tests run in
`make check`.

## Controlled acceptance identities and observations

Command: `make work-intake-acceptance` (exit 0 in this session). It used local
PostgreSQL, MongoDB, RustFS/BC06, one Scrapy worker image, the Go gateway and the
controlled internal source. The M4 lifecycle prepared and approved the exact
source/policy, Input Contract, payload contract, Playbook, BotBuild and
BotRelease, then installed trusted authority. The worker image compiled one
BotBuild once and that same build, release and runtime processed the three
different Inputs plus one intentional reacquisition. No external source was
contacted.

Shared identities:

- Playbook: `examples/use-cases/controlled-work-intake/capture-playbook.yaml`,
  v3, one `limit` query parameter and one GET response.
- SourceRevision: `controlled-work-intake-v1`; source and policy allow only
  `limit` values 1, 2 and 3 for successful responses.
- Input Contract: `controlled.input.limit@1.0.0`, digest
  `sha256:ce76be721604553e02e0b7bb04dddd6081508ca97b6b7e1e03fc9571df9a23d3`.
- Payload contract: `controlled-work-intake.record@1.0.0`, digest
  `sha256:89a946e40874f93195325f2ea3ef16d13167a80f0335a41b412b63d6d262fbf8`.
- BotBuild ID: `fb279f437fb798212a59074d5b7c44f81fd287491c7e3ebdbc249e38e1c4d509`;
  manifest digest `sha256:b452d020546bee9754cabb4a96df56f8d9dd1066028349602497553063e8b034`.
- BotRelease: `controlled-live-release-ac09d244eb91` (approved for all four
  successful observations, then revoked for the negative admission check).
- Runtime image: `sha256:996d0196554f7d276863808c67de1c516685a214d408d98894fd1d00c3222829`.

The independent BC05 read compared published record values with the test oracle;
BC06 retrieval checked each ArtifactRef size and digest; publication receipts
matched BC04's terminal receipt. The run also verified distinct output and
observation identities across the intentional reacquisition, although the raw
artifact bytes had the same digest and custody retained distinct artifact
references.

| Logical Input | Parameter digest | Submission | CaptureRun / CaptureAttempt | Authorized target / FetchEvidence | OutputSet / PublicationReceipt | ArtifactRef / retrieved digest | Independently read value |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `input-A` | `sha256:156fedef9af97685d0e6f43eebe55d8ae67fcbf34b2fea23c2b1af061b4bcdf0` | `2f243be3-658d-55bd-98dd-f7a03ad4e65b` | `1ed34f95-c6e0-5298-b1f4-0df629bc4397` / `67f148e1-fc5c-5b7b-a83b-610ef1402ff6` | `...?limit=1`; `bde468b72737b640eec6a4a569c4248b` | `set-1e7f7296b4438485b5414d01c7c7b3931d312282b8125e19d10e1f68df22f135`; `pub-bfa6a9dd42b1d7631c2ad121596d41ba788c1bb6569282581624708fe7b5b26b` | `artifact-dc65166fe94a2e430d72b16d0e64507a767ab5bddc0a03d34df9a321f394a1b2`; `sha256:39c5007a4c327f24ba25a9bad5630b1e48e0daa4f8e32c78475db16ee14944a1` | `parameter-1`, quantity `1`, price `1.25` |
| `input-B` | `sha256:649c187d840a97ceebfecf4a5e8a1121655263c52487be7d5cd1970fe17e1bd3` | `f73cd765-b36c-5b16-8d78-3d1ef47f4760` | `aebfce60-f8c4-5552-978d-f9ebf532303d` / `159750b2-565c-5c0f-88fe-dbe4085e2186` | `...?limit=2`; `1baa86bfc05949c448165925e9883e29` | `set-2310d4f107527ec6fbba94e7cf2a4641cc10c230b56196c2545b476f83696aef`; `pub-9193577c62b52a85a6f13e30e2ee668b650246e3a2efcddb646a16d4aef64be8` | `artifact-9f0ec9747cca9a1ca2b628651dd82bc1d78a9c1166b3a1937538d420b8c491c6`; `sha256:b908610cbef9071a2937e7db7b4ec859a2a53d6787afb99e707f267bab613d84` | `parameter-2`, quantity `2`, price `2.25` |
| `input-C` | `sha256:a7175ed78ef07ac44431637962e4f5f94a66ffd04e15d47f6a1547a155491fc3` | `86d7dff9-4529-57c1-a4a5-1ac13cee3eed` | `b95c2c71-0128-53d2-82c9-235121ee548d` / `7588fd8e-2726-5e97-9e8b-cfe66260bef9` | `...?limit=3`; `698dfe134e9b40afb5d3bfea7f20d34d` | `set-9619251334ea1fea5e0a686b0d5aacb3a4a7b47d9dc9e19ebff120f2c89ac22e`; `pub-c92eee816ad0d2ca5de9ad8d278046fc04555aa759b6ceca87135034c97d1362` | `artifact-c9249732c7272e583a0fb488b15856cc2227dc15ece8fd60628c2bce93e7d948`; `sha256:43ee7d602e84632c3869178450df73844c6f6e265506a14e949fe6b7ca0f6f73` | `parameter-3`, quantity `3`, price `3.25` |
| Intentional reacquisition of `input-A` | same as original A | `d4b1ed80-255c-5792-86a8-cb98ef398b11` | `13184905-7b26-5287-aaa5-f1bac799fae9` / `ad90e17a-5f23-5781-bd93-5daa4f5a9516` | `...?limit=1`; `d5cd4351b787c5609f4864260d9bd285` | `set-83e32d4dad396f08bd9dee22315281a8f1e3ecdc77342f240fa75c61a1becbe9`; `pub-b817e96a3cb45a76efdfcda190e2458bc375564e4489f2dc8b4b70fd7a962f5f` | `artifact-e5c3a7d33820e0566fa0d985ac57d78f2bccbf5d4ad955215ad63313d61d6c2f`; same content digest as A | `parameter-1`, quantity `1`, price `1.25`; distinct OutputSet, output ID and observation ID |

In every row the full target was `https://api.fixture.test/v1/items?...`.
Provenance in each published record matched that exact target, and BC13
inspection resolved its Submission to the corresponding BC04 CaptureRun and
authoritative succeeded outcome.

Independent BC05 output identities were also distinct across all four
successful observations:

- Input A: output `out-71e5e9de1b8fdfd66ed0cceafb0264416338534dd70c2c3cf14534667c29ec87`, observation `obs-f178a4f6dd7289f8bc47a026882a45789625502b0f853bfba31a25a397848a91`.
- Input B: output `out-68c03b44db385dfb9e416ff5d3c2b2d27951ecbdecde74f24ad7ab165acfffc5`, observation `obs-99297f9f613e2dc85af2b244ed0b4a479fc1927ad3f416501e7b793432313037`.
- Input C: output `out-7e29c06cc4225e07c056374222174543952a36bb5c6c719df43e385c5d56d342`, observation `obs-efdccd39fd61100e9de15b773e3fd1dbb18ad93143e96419943a0915594a0289`.
- Reacquisition A: output `out-3cc0baa7d0b48464ba486a4c70a658b061b8502f3ebf6c6b964474e36f4649ee`, observation `obs-cb41cfaa988dd441e517be39b7aa4555b1bf91248f7ac10140400443da2e1e07`.

## W1–W18 acceptance matrix

`PASS` means the stated level passed in this local session; it does not imply a
remote CI result. The injected acknowledgment loss is a deterministic exception
after the BC04 commit, not a process-crash simulation.

| ID | Test / validation level | Actual components and expected → observed | Durable evidence / failure injection | Status and limits |
| --- | --- | --- | --- | --- |
| W1 Reusable composition | `make work-intake-acceptance`; integrated | `compose_work_intake` submit/dispatch/inspect/cancel APIs; BC13 → M4/BC04 boundary used outside domain logic | same composition reconstructed over PostgreSQL and BC04 | PASS; no universal CLI or worker fleet |
| W2 Input A | integrated | PostgreSQL BC13, M4 release, BC04, Scrapy, gateway, trusted import, BC06, BC05; exact limit 1 → published `parameter-1` | IDs and receipt above | PASS |
| W3 Input B | integrated | same shared build/release/runtime; exact limit 2 → published `parameter-2` | IDs and receipt above | PASS |
| W4 Input C | integrated | same shared build/release/runtime; exact limit 3 → published `parameter-3` | IDs and receipt above | PASS |
| W5 Submission replay | unit + PostgreSQL | same scoped key and intent, cross-tenant scope, fresh store replay → original durable identity | `make work-intake-tests`; `make work-intake-postgres-tests` | PASS; restart coverage reconstructs adapter/store, not killed OS process |
| W6 Conflicting intent | unit + PostgreSQL | changed parameters or release under same scoped key → explicit conflict | PG same-release-key conflict and concurrent identical replay | PASS; values do not globally deduplicate Inputs |
| W7 Dispatch idempotency/concurrency | PostgreSQL + integrated | bounded concurrent claims are exclusive/fenced; same dispatch identity resolves one BC04 run | `test_concurrent_dispatch_claims_are_exclusive_and_fenced`; integrated ACK-loss run ID unchanged | PASS; no distributed queue |
| W8 Admission ACK loss | integrated | real BC04 admission commits, injected acknowledgment is lost, fresh coordinator/composition resolves original receipt → same run, no second run | `WORK_INTAKE_ADMISSION_ACK_LOSS_RECOVERED`; run `afae6555-5173-5c1f-8a63-a4383e714955` | PASS; deterministic exception, not process-crash evidence |
| W9 Pending recovery after restart | PostgreSQL + fresh OS subprocess | a new Python process claims the original pending durable Submission from PostgreSQL; fresh coordinator/composition also recovers W8 | `test_pending_work_is_claimable_from_a_fresh_process`; no dispatcher state is shared in memory | PASS; this proves process reconstruction, not an OS crash or kill of a running process |
| W10 Intentional reacquisition | integrated | same logical Input A and parameter → new Submission, CaptureRun, OutputSet, output and observation IDs | reacquisition row above; raw content digest remains equal | PASS; no backfill orchestration |
| W11 Invalid parameters | application + contract tests | exact immutable contract validation, canonical bytes, closed object, type/value/size checks, strict duplicate-key parsing → reject invalid binding | `make work-intake-tests`, contract-resolution and acquisition-application tests | PASS at validation boundary; invalid work creates no published output |
| W12 Unauthorized parameter/target | worker + BC04 + gateway tests | changed value, repeated query, extra query, broad-policy-allowed but execution-unauthorized value → reject before prohibited source request | zero-source-hit gateway test; BC04 single-response integration; `limit=2` substitution test | PASS at component security boundaries; integrated successful cases only issue exact authorized targets |
| W13 Worker substitution | worker unit | replace `limit=3` with another contract-allowed `limit=2` → capability digest mismatch | `test_input_parameter_file_must_match_the_bc04_capability_digest` | PASS |
| W14 Revocation/eligibility | integrated M4 + BC13 | submit while eligible, install revocation before new admission → rejected, no CaptureRun; prior output remains readable | `WORK_INTAKE_REVOCATION_ACCEPTED`; authority generation 185; M4 authority generation 186 | PASS; receipt recovery remains separate from new admission authorization |
| W15 Cancellation | integrated + BC13/BC04 tests | cancel pending before first claim; after ACK ambiguity reconcile then delegate to BC04; replay same cancel → coherent cancelled outcome | `WORK_INTAKE_CANCELLATION_ACCEPTED`; BC04 finalization cancellation race tests | PASS; cancellation does not undo already-issued external requests |
| W16 Acquisition failure | integrated | valid admitted parameterized run receives controlled malformed source response → BC04 failed, no OutputSet publication receipt | `WORK_INTAKE_ACQUISITION_FAILURE_NO_PUBLICATION`; run `fed8a433-c442-5f54-b8c2-4d4b731660a6`; diagnostic `JSON_DOCUMENT_INVALID` | PASS; failure occurs before trusted import/custody, so no BC06 artifact is created |
| W17 Input-to-output provenance | integrated independent read | Submission → run/attempt/release/build → trusted FetchEvidence → OutputSet/receipt → ArtifactRef bytes/digest; each value correlated | output/observation IDs are distinct across all four successful observations; A/B/C contents match test oracle | PASS |
| W18 CI gate | local workflow validation | `.github/workflows/ci.yml` includes `make work-intake-acceptance`; YAML parsed and gate ran locally | workflow parser passed; integrated local command exit 0 | PASS prepared; remote CI has not run because branch is unpublished |

## Regression evidence executed in this session

- `make check`: passed; 220 root tests, 26 environment-gated skips. Skips are
  reported as skips, not passes. Includes Ruff, formatting, docs, migration,
  architecture, package/component tests and fixture suite.
- `make work-intake-tests`: 18 tests passed.
- `make work-intake-postgres-tests`: 10 tests passed against the configured
  PostgreSQL service.
- `make execution-coordination-finalization-tests`: 13 BC04 PostgreSQL tests
  passed; actual BC04/PostgreSQL → BC05/MongoDB finalization passed with two
  reconciliation clients.
- `make acquisition-application-tests`: 11 tests passed, including trusted
  execution parameter target/digest, duplicate property, query injection and
  URL encoding checks.
- `make acquisition-gateway-go-format-check acquisition-gateway-go-vet
  acquisition-gateway-go-test acquisition-gateway-go-race
  acquisition-gateway-go-build acquisition-gateway-go-vuln`: passed; race suite
  passed and `govulncheck` found no vulnerabilities.
- `make work-intake-acceptance`: passed end-to-end with three distinct Inputs,
  one reacquisition, ACK-loss recovery, cancellation, revocation, malformed
  response failure, independent MongoDB reads and BC06 artifact retrieval.
- The final M5 command's M4 authority/revocation acceptance passed. Earlier in
  this session, the standalone M4 lifecycle acceptance also passed.
- `.github/workflows/ci.yml` YAML parse and M5-gate presence check passed.
- `git diff --check` passed. No remote CI has been claimed.

Other protected execution, live JSON/HTML, pagination, redirect, compiler
conformance, gateway-container, and M4 regression results are recorded alongside
their exact command status in the final session report.

## Remaining limits and readiness

The parameterized profile is limited to one scalar query key and one GET
response. General pagination, continuation, arbitrary URL templates, dynamic
expressions, external source acquisition and worker-fleet orchestration remain
out of scope. The controlled source's malformed response is explicitly
test-only. The acceptance simulates ACK loss deterministically and reconstructs
application/coordinator objects over durable state; it does not claim an
OS-level crash occurred. Remote CI has not run because the branch is local.

M5 is ready for human PR review. The final local regression commands completed
successfully; the root suite ran 220 tests with 26 environment-gated skips.
Remote CI status is determined by the corresponding GitHub PR. ADR 030 remains
Proposed and requires human architecture approval.
