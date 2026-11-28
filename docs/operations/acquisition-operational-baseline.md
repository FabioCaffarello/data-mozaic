# Acquisition development and operations

This is a local controlled development profile. It does not provision the
protected Scout environment, grant source approval, or demonstrate an actual
agent session. Scout readiness can remain NO-GO while deterministic
developer-authored acquisition is available.

## Reproducible lifecycle

Run from the principal checkout with the repository's reviewed local
environment files already present. Do not reset database or object-store
volumes to make an acceptance pass.

```sh
make sync
make execution-coordination-durable-migrate
make work-intake-migrate
make acquisition-output-mongo-connectivity
make artifact-custody-up
make acquisition-capabilities
make acquisition-preflight
make acquisition-operational-acceptance
```

The migration and connectivity targets report readiness for their respective
owners. `make artifact-custody-up` starts the preserved RustFS/BC06 profile and
waits for its configured health checks. The controlled live acceptance starts
and stops its ephemeral Gateway/source containers while leaving named
volumes intact. `make db-down` and `make artifact-custody-down` stop services
without `-v`; never substitute a volume removal command.

`make acquisition-preflight` composes read-only owner connectivity, migration,
contract, release, Gateway capacity, custody persistence and operator-identity
checks. It is advisory: it does not reserve capacity or replace admission-time
authorization. Missing or unreachable checks produce `not_ready` diagnostics.
Protected Scout readiness remains separate. `make scout-dev-status` is a separate
protected authoring preflight: a NO-GO source approval, absent MCP process or
missing managed policy is expected to remain visible and must not be bypassed.

## Operational capabilities

Use `make acquisition-capabilities` to print the current BC15 capability
catalog. At this baseline:

- Structured JSON and HTML profiles support bounded extraction. HTML can use
  the existing bounded link pagination graph; pagination consumes separately
  authorized Gateway requests and has explicit page/request limits.
- Raw-only v4 performs a single bounded GET with no caller parameters,
  redirects or continuation and keeps the 2,000,000-byte profile limit.
- Each live response is policy-bounded before network dial. The controlled
  Gateway Compose profile has 67,108,864 spool bytes, 500 evidence entries,
  and eight global active source fetches. Playbook policy further limits
  per-policy concurrency (at most four); controlled live examples use one.
- Evidence retrieval admits four simultaneous body readers. BC06 write/read
  limits are separately configured by the custody service. The preserved local
  BC06 profile is configured for 104,857,600 bytes per artifact, four
  concurrent transfers and a 440,401,920-byte aggregate upload temp budget.
  Those provider limits do not widen Playbook/profile policy. The current BC06
  object adapter uses a bounded temporary file and one conditional S3
  PutObject; multipart is unsupported.
- The reusable `TrustedAcquisitionApplication.inspect_evidence_release` and
  `release_published_evidence` operations verify exact BC04 completion and
  reservation, BC05 published membership and receipt, and BC06 logical artifact
  custody before asking Gateway to release a body. The release credential is
  separate from import/read credentials. The acceptance harness calls this
  application operation; it does not make eligibility decisions itself.
- Gateway keeps at most 500 active evidence entries in the controlled Compose
  profile. Released metadata moves into a separate bounded archive
  (default 16 MiB, controlled by `GATEWAY_MAX_EVIDENCE_ARCHIVE_BYTES`); it no
  longer consumes active entry slots. Archive metadata remains finite and
  durable. Authenticated `/internal/v1/resources` reports `spool_bytes_used`,
  `spool_entries`, their configured limits, and archive used/max/available
  bytes plus `spool_archive_exhausted`. If archive capacity is exhausted,
  release returns `507 evidence_archive_capacity_exhausted` and the active
  body and entry remain accounted. Historical provenance remains
  queryable through the Gateway metadata interface, but body bytes are not
  available there after release. Pending/unresolved work retains its body.
  The finite archive has no purge or compaction automation; M9 does not claim
  indefinitely sustainable historical storage.
- Release has been exercised for raw-only v4, structured HTML and parameterized
  structured JSON when each FetchEvidence page maps to a published,
  custody-verified ArtifactRef. Pagination and planned-window pagination are
  part of the finite M9 acceptance. This is not a general retention service.
- Capacity is local to one Gateway instance and has no distributed sharing or
  HA guarantee. A process restart rebuilds usage from safe spool files.
- Redirect policy is deny by default; the existing structured live profile
  can revalidate a bounded redirect chain. Raw-only v4 has no redirects.
- No 8, 32, 64 or 100 MiB source capability is implied by BC06's larger local
  storage setting or historical benchmark measurements.

See [ADR 032](../architecture/decisions/032-bounded-raw-artifact-transfer.md),
[ADR 034](../architecture/decisions/034-gateway-evidence-body-release.md),
and the [M7 resource assessment](../architecture/assessments/acquisition-resource-maturity-2026-11-27.md)
for exact contract bounds and measured workloads.

## Recovery and inspection

- Work Intake inspection, dispatch replay and claim release remain BC13-owned;
  use the Work Intake application operations and `make work-intake-tests` for
  the local contract suite.
- BC04 reconciliation remains `make execution-coordination-reconcile` or the
  tenant/run-scoped `make execution-coordination-recover-run TENANT_ID=…
  RUN_ID=…` operation. It does not replace BC04 finalization.
- BC06 residual operations are inspected with its public authenticated
  artifact metadata, verification, pending-event interfaces, bounded
  tenant-scoped `GET /draft/v1/artifacts?limit=…&cursor=…` inventory and
  `make artifact-custody-reconcile` operation. Inventory returns logical
  artifact/operation identity, custody state and timestamps, not storage
  locations. Reconciliation is non-destructive; no cross-context orphan
  deletion is supported. Disposition without a known published reference is
  explicitly unknown.
- BC05 normal reads return only published OutputSets, through
  `AcquisitionOutputApplication.read_published_outputs` with bounded cursors.
  Sealed/staging memberships are never exposed as raw consumer results.
- Acquisition inspection is owner-specific. A composite report assembled
  from BC13, BC04, BC05, the Gateway and BC06 can observe those stores at
  different instants and must label missing/unresolved components; it is not
  a globally consistent snapshot.

For a known tenant-scoped Submission, run the read-only composite inspection:

```sh
make acquisition-inspect \
  ACQUISITION_INSPECT_ARGS="--tenant-id <tenant> --submission-id <submission>"
```

The command reads BC13 and BC04 records, a bounded BC05 published page and
referenced BC06 metadata. FetchEvidence IDs are not indexed in the current
public BC05 contract, so that correlation is reported unresolved. BC06 exposes
bounded tenant-scoped inventory pagination; inventory results without known
published references have unknown disposition. No deletion is inferred from
absence in a page.

For exact published raw-only v4 evidence, the reusable operator entrypoint
first inspects eligibility and requires `--release` for the explicit mutation:

```sh
make acquisition-release-evidence \
  ACQUISITION_RELEASE_ARGS="--tenant-id <tenant> --run-id <run> --fetch-id <fetch-id>"
make acquisition-release-evidence \
  ACQUISITION_RELEASE_ARGS="--tenant-id <tenant> --run-id <run> --fetch-id <fetch-id> --release"
```

Load `GATEWAY_HTTP_URL`, both distinct Gateway credentials,
`MOZAIC_OPERATOR_ID`, and `MOZAIC_EVIDENCE_RELEASE_OPERATORS` from the trusted
operator environment. The command resolves FetchEvidence metadata through
Gateway, then delegates eligibility and release to the trusted acquisition
application. It accepts no caller-provided completion flag, digest or artifact
reference. Structured release remains available through the trusted importer
when its verified page bundle is present; this raw-only CLI does not invent the
missing structured page mapping from historical metadata.

## Developer-authored lifecycle

The existing operator CLI supports immutable source registration/resolution,
Playbook and verified build registration, candidate creation, explicit release
approval and authority publication. Start with its actual command syntax:

```sh
uv run --locked --all-packages acquisition-lifecycle --help
uv run --locked --all-packages acquisition-lifecycle register-source --help
uv run --locked --all-packages acquisition-lifecycle register-playbook --help
uv run --locked --all-packages acquisition-lifecycle register-build --help
uv run --locked --all-packages acquisition-lifecycle create-candidate --help
uv run --locked --all-packages acquisition-lifecycle approve --help
uv run --locked --all-packages acquisition-lifecycle publish-authority --help
```

The CLI operates on caller-supplied immutable documents and uses the existing
reviewer allowlist for release decisions. Compilation remains
`collector-engineering`; this path does not require Scout or an LLM. BC13
exposes a reusable `WorkIntakeOperations` facade for submit, bounded
dispatch/reconcile, inspect, execution inspection and cancel. The acquisition
application also exposes `OperationalAcquisition`, which coordinates these
operations with BC04 attempt/capability issuance and trusted result import.

### Configuration-backed operator workflow

Install the locked workspace entrypoint with `make sync`. Before use, register
the exact SourceRevision, contracts, Playbook, verified BotBuild and approved
release using `acquisition-lifecycle`; publish its signed authority. Build the
Compose worker image using the same immutable Playbook and runtime inputs used
for the registered BotBuild. At execution, the CLI compares the local worker
image ID with BC04's exact approved runtime digest and fails closed on a
mismatch.

Build the already-registered runtime under the configured Compose project
before dispatching an admitted run:

```sh
docker compose --project-name "$MOZAIC_LIVE_COMPOSE_PROJECT" \
  --file deployments/compose/acquisition-live.compose.yaml \
  --profile operator build operator-worker-scrapy-live
```

The same project name and exact Playbook/fixture build inputs must be used when
the immutable BotBuild runtime digest is registered and approved. A new or
different image does not inherit release authority.

Load the existing owner environment files into the operator's protected shell
environment. Do not commit a populated configuration file:

```sh
set -a
. ./.env.execution-coordination
. ./.env.work-intake
. ./.env.mongodb
. ./.env.artifact-custody
set +a
```

If the repository's Make variables `MONGO_ENV_FILE` or `BC06_ENV_FILE` select
preserved sibling configuration files instead of these local defaults, source
those exact configured files. Required additional settings are documented in
[`acquisition-operator.env.example`](acquisition-operator.env.example). Secret
material stays in the existing local secret/configuration files or owner-only
key files. `MOZAIC_OPERATOR_ID` labels the Work Intake dispatcher; it does not
create authority. Approval remains with the lifecycle reviewer allowlist,
admission with the signed BC04 authority, and execution capabilities are
issued by BC04 for the current attempt. The capability file and source
credentials are not passed into the protected worker.

An Input is a bounded JSON document with exactly the following properties:

```json
{
  "tenant_id": "tenant-example",
  "input_id": "input-example",
  "source_id": "source-example",
  "source_revision_id": "revision-1",
  "input_contract": {
    "contract_id": "input-contract",
    "version": "1.0.0",
    "digest": "sha256:<exact-installed-contract-digest>"
  },
  "idempotency_scope": "operator",
  "idempotency_key": "request-unique-key",
  "release_id": "approved-release-id",
  "parameters": {}
}
```

Use the narrow operator commands; each invocation composes the same BC13,
BC04, Gateway, trusted importer, BC05 and BC06 owner interfaces:

```sh
uv run --locked --all-packages acquisition-operator submit --document input.json
uv run --locked --all-packages acquisition-operator dispatch --tenant-id tenant-example --limit 10
uv run --locked --all-packages acquisition-operator execute --tenant-id tenant-example --submission-id <submission-id>
uv run --locked --all-packages acquisition-operator inspect --tenant-id tenant-example --submission-id <submission-id>
uv run --locked --all-packages acquisition-operator reconcile --tenant-id tenant-example --submission-id <submission-id>
uv run --locked --all-packages acquisition-operator cancel --tenant-id tenant-example --submission-id <submission-id>
```

`run --document input.json` is a bounded submit/dispatch/execute shortcut. The
acceptance exercises the individual commands in separate fresh processes,
including two distinct canonical parameter bindings against one approved
release, submission replay, protected worker execution, trusted import,
BC05/BC06 publication, inspection, reconciliation, cancellation and intentional
reacquisition. Published records and artifact bytes are verified independently
through the public BC05 and BC06 APIs. Run this controlled journey with
`make acquisition-operator-acceptance`; the complete finite gate is
`make acquisition-operational-acceptance`.

Do not expose the CLI's owner credentials to authoring agents or worker
containers. The command does not grant unrestricted network access: the
registered BC04 release and current execution capability remain checked by the
Gateway for every fetch.

## Independent consumption

Downstream consumers use BC05's published read API to retrieve record,
artifact-reference or composite outputs. They retrieve bytes from BC06 using
the logical `ArtifactID` and authenticated tenant-scoped read interface, then
verify the declared size and digest. Do not use Gateway body files, physical
object keys, storage URIs or expiring URLs as consumer authority. The worker or
agent is not needed for published reads.

## First-source onboarding checklist

This checklist is evidence gathering, not source approval. Do not contact or
acquire from a real source without the existing authorized SourceRevision and
release review.

1. Record the proposed Source identity and immutable revision owner.
2. Confirm who authorized access and record applicable terms, robots/access
   restrictions, authentication requirements and quotas.
3. Assess endpoint stability, TLS, redirects, pagination and expected changes.
4. Select an existing BC15 Playbook/profile and modality; document any
   mismatch instead of creating a new profile in this increment.
5. Resolve the exact Input Contract, Payload Contract (if structured),
   artifact representation and source revision.
6. Record allowed methods, exact paths/query parameters, request frequency,
   page/request budget and redirect constraints.
7. Confirm declared and observed response-byte limits fit the current profile,
   Gateway spool and BC06 transfer bounds.
8. Define expected recovery after interruption, cancellation, custody failure,
   partial staging, publication acknowledgment loss and replay.
9. Specify evidence metadata and body retention needs, including whether
   published BC06 artifacts satisfy historical byte access.
10. Identify a downstream consumer that reads only published BC05 outputs and
    authenticated BC06 artifacts; define independent digest/provenance checks.
11. Obtain separate SourceRevision, Playbook, release and reviewer approvals
    through existing authorities. This checklist does not approve any of them.
12. Keep actual protected agent-assisted authoring gated by A6; deterministic
    developer authoring does not waive that operational gate.
