# Integrated fixture acquisition — implementation evidence

**State:** local, uncommitted implementation on `feat/acquisition-fixture-integration`; no Gate C PR has been opened. The verified base is main `1db31b3e32390202496befff49bef7c05bd10769`, which includes BC04 PR #19. ADRs 021–024 remain Proposed.

## Flow and ownership

The local operator command compiles the existing `html-simple` CapturePlaybook into a deterministic BotBuild, assembles and verifies its immutable catalog, builds the existing Scrapy worker image, and starts a durable BC04 CaptureRun and attempt. The protected executor starts the worker with `--network none`, a read-only root filesystem, non-root user, read-only catalog and fixture mounts, writable bounded output mount, CPU/memory/PID limits, and the existing local Docker trust boundary.

The worker writes only its bounded records, runtime report and raw-reference manifest. The trusted host result collector preserves the validated `ExecutionReport` beside those files. The importer accepts a closed four-file bundle, checks strict JSON and duplicate-key handling, report/run/attempt/build binding, full bundle bytes and digests, independent expected records, fixture index membership, body size/digest, raw-reference provenance, path traversal and symlink rejection. It does not trust tenant/source/run/build identities from worker payloads.

The selected fixture produces two record observations. The importer maps them to BC05 `CompositeOutput`s and uses stable identities scoped to run, attempt, fixture page and record position. Before staging, it reserves and uploads the immutable HTML fixture body through BC06 and verifies the exact tenant, logical artifact identity, digest and size using BC06's public HTTP contract. The declared bytes are the synthetic fixture response-body representation. They are not original HTTP wire bytes, a live response, or an artifact emitted by the worker container.

`tooling/fixture_acquisition.py` is deliberately a fixture-specific trusted adapter, not a generic ingestion importer: it validates the current worker bundle/report and immutable fixture identity, uses independent `html-simple` expected records and schema, then performs this example's record-to-composite mapping. These semantics are not installed in BC04/BC05 domains. Future live acquisition can reuse bounded strict bundle/path/digest validation and public BC05/BC06/BC04 ports, but it must provide a live-response metadata contract, independently bound release/policy identity, per-contract payload validation and an explicit source-to-output mapper. No universal mapping framework is justified by one fixture consumer.

BC05 creates, stages and seals the bounded OutputSet. BC04 then reserves finalization and signs an Ed25519 grant for the exact set/manifest. The MongoDB publication transaction records the output-set publication, receipt and event; a synthetic lost acknowledgment leaves BC04 pending. A fresh reconciliation process retrieves the durable BC05 receipt and completes BC04. Another fresh process revalidates and replays the persisted result bundle; the published outputs and BC06 bytes are read through their public interfaces and checked against the fixture digest.

## Local operator command

Start PostgreSQL, the MongoDB replica set, and BC06/RustFS using their established owner worktrees and ignored local environment files. Keep the existing volumes and credential files. From this worktree, with the preserved environments available at the example paths, run:

```sh
make execution-coordination-fixture-acquisition \
  MONGO_ENV_FILE=../data-mozaic-mongodb/.env.mongodb \
  BC06_ENV_FILE=../data-mozaic-artifact-custody/.env.artifact-custody
```

The target applies the BC04 migration, probes and idempotently provisions BC05, then requires `BC04_LOCAL_FIXTURE_PROFILE=1` for the composed fixture-only run. It builds the BotBuild and image from a temporary directory below ignored `.local/`, persists the verified immutable assembly under `.local/execution-coordination-fixture/assemblies/`, and retains result bundles under `.local/execution-coordination-fixture/results/` so a process can replay them. It does not delete results or platform volumes. The normal protected executor's network-deny boundary remains enabled.

## Validation evidence

The final successful local command was:

```sh
make execution-coordination-fixture-acquisition \
  BC06_ENV_FILE=../data-mozaic-artifact-custody/.env.artifact-custody
```

It returned exit status 0 and reported run `10492415-b0d7-4a68-9fad-77d8768311c2`, two outputs, one fixture page, manifest `sha256:adac3c5fb89bfc1c623c4702dae454a864db0874a5cd068df5d7024da1babc88`, `network=none`, `replay=fresh-process`, and `publication=BC04-authorized`. The fixture body was 274 bytes. Before staging, the same real-store run rejected altered-digest and stale-attempt report bindings and confirmed no corresponding BC05 OutputSet had been created. This is a synthetic local run, not production readiness or source evidence.

Nine focused root tests pass for valid bundles and for duplicate/invalid JSON, duplicate report properties, unexpected files/symlinks, path traversal, symlinked fixture roots, changed fixture/output digests, counter mismatch, unknown references and mismatched independent expectations. The end-to-end command additionally exercised a real BotBuild and Scrapy container, BC04 PostgreSQL, BC05 MongoDB replica set, BC06 PostgreSQL/RustFS HTTP custody, output publication, lost acknowledgment reconciliation, fresh-process replay, published reads and artifact digest verification.

Earlier attempts exposed and corrected three integration-harness mismatches before the final successful run: macOS temporary-directory symlink ancestry rejected by the existing fixture compiler, the distinction between worker `temporary_ref` and BC04 execution UUID, and the distinction between worker-file byte count and the supervisor-preserved report byte count. One run published and completed before a final-verifier digest-prefix bug was corrected; that synthetic local data was preserved. A later run completed with all final checks. No database or object volume was reset or cleaned.

One earlier synthetic attempt, `b59bbf46-3c3f-4e09-bc66-ef6fa2afd94e`, was created before the importer check was corrected and did not publish data. The BC04 implementation now provides `DurableExecutionCoordinator.recover_expired_run(tenant_id, run_id)` and the scoped operator command `make execution-coordination-recover-run TENANT_ID=<tenant> RUN_ID=<uuid>`. It uses PostgreSQL time and locks only the identified run, then applies the existing lease/deadline transition and retry-budget decision. The exact local run was re-inspected before invoking the command; its resulting state and eligibility are recorded in the validation amendment below. Its result bundle is retained under the ignored local result directory.

## Limits and recovery boundary

- This demonstrates fixture execution only. No external source exploration, live HTTP access, Playwright, parser service, or production authorization was used.
- The selected compiler output supports records and raw fixture references; the example maps the trusted fixture body to a composite artifact. It does not claim that the worker emitted raw response bytes.
- The importer uses the BC05 application and BC06 public HTTP contract. It does not access BC05 MongoDB internals or BC06 PostgreSQL inventory.
- Replaying the same bundle reuses artifact, OutputSet and operation identities. A new run/attempt produces new observations.
- The fixture importer uses the durable Run creation timestamp as this synthetic observation's stable `observed_at`; it is not a measured source observation time. A production worker result contract needs an explicit trusted observation-time semantics.
- Temporal meanings are distinct: Run creation is the request acceptance time; Attempt start is when BC04 claims execution; acquisition time is when the worker performs its fixture operation (not measured by this fixture path); observation time here uses Run creation only for deterministic synthetic replay; source reference date is absent; ingestion time is when trusted import accepts the bundle; publication time is the BC05 receipt timestamp.
- The result bundle and verified assembly are retained locally, but BC04 does not persist a worker lease token that allows a fresh process to continue an attempt after a crash before finalization reservation. In that failure window the lease must expire and BC04 must decide whether to retry; the old attempt cannot finalize as current. This increment does not add a durable importer job or a new BC04 recovery state machine.
- BC04/BC05 publication remains non-ACID across PostgreSQL and MongoDB. Safety comes from the durable finalization reservation and exact grant; unresolved authorization favors safety over availability. There is no exactly-once event delivery, production service authentication, BC11 authorization, HA, or live acquisition.
- The Gate C CI step is present locally but has not run remotely because this branch is unpublished.

## Scoped recovery review amendment — 2026-11-25

The application operation is tenant/run scoped. Its PostgreSQL adapter locks the identified run, evaluates lease and run-deadline expiry using `clock_timestamp()`, and applies the same state transition as bounded global expiry. If no run matches, the application reports `CaptureRun does not exist`; if it is not eligible, its current snapshot is returned unchanged. A finalization-pending run is excluded, preserving its durable reservation. Real PostgreSQL integration tests cover wrong-tenant isolation, recovery of an expired lease, retry-budget handling, idempotent repeat, stale lease rejection, deadline timeout and no effect on an unrelated run. This does not recover importer work or grant a new attempt automatically.

The identified local run was rechecked read-only before recovery: its state was `running`, current attempt `running`, lease expired, no reservation, one of two attempts consumed, and no MongoDB OutputSet referenced that run. The scoped command completed with exit 0 and reported `state=timed_out`, `generation=1`, `attempt_state=expired`, no lease and no reservation. The run deadline had also elapsed, so BC04 correctly chose the existing terminal timeout transition rather than retry eligibility. No other run or data store was modified. Its ignored result bundle remains preserved for inspection.

## Local configuration and worktree preservation

The current integrated Make target still references owner-local environment files: `.env.execution-coordination` in this worktree, MongoDB's `.env.mongodb` selected from the repository or the historical sibling `../data-mozaic-mongodb`, and BC06's `.env.artifact-custody` selected through `BC06_ENV_FILE` (the documented existing sibling worktree path is an example). These files and their values were not inspected or copied during this amendment; existing credentials and volumes remain intact. A fresh checkout should create isolated credentials from each owner-provided `.env.example` and use isolated Compose project/volume names. Reusing existing persistent volumes requires the matching existing credentials and endpoint configuration; do not generate replacement credentials against those volumes. A stable repository-level env directory or explicit file-path overrides can remove sibling-worktree assumptions in a later operational PR, but no configuration migration is part of this change.

Validation confirmed a specific MongoDB Compose path dependency: invoking `make acquisition-output-mongo-tests` from the Gate A worktree selected the sibling `.env.mongodb`, but the relative Compose bind for `.local/mongodb/keyfile` resolved under Gate A's checkout and failed because that owner-local keyfile is in the MongoDB worktree. The named volume remained present and the container exited before MongoDB startup. Starting MongoDB through `make -C ../data-mozaic-mongodb acquisition-output-mongo-up` reused the owner configuration successfully; BC05's 17 real replica-set tests then passed from the Gate A code. A fresh checkout must provision its own isolated MongoDB keyfile and credentials, or explicitly pass the owner Compose file path along with its matching env file. The cross-worktree defaults are convenience only, not a portable setup convention.

Historical worktrees should be archived only after their PR/source-tree integration is verified, the worktree is clean, and all local environment and volume dependencies are listed. For the current Gate A review preserve the feature worktree, branches, stale PR #8 metadata and every persistent volume; do not prune or delete anything.
