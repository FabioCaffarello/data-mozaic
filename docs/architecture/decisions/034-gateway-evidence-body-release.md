# ADR 034 — Gateway evidence body release after durable publication

- Status: Proposed
- Date: 2026-11-27
- Owners: Acquisition Gateway and Trusted Acquisition Application
- Scope: M9.1 operational evidence lifecycle

## Context

BC04 owns current attempt, generation, finalization reservation, terminal
execution state and reconciliation. BC05 owns OutputSet membership, published
visibility and PublicationReceipt. BC06 owns artifact identity, verified bytes,
custody and receipt. The Gateway owns FetchEvidence metadata and temporary
response bodies. Its body spool is finite (64 MiB and 500 evidence entries in
the controlled live Compose profile); body bytes otherwise remain until manual
filesystem intervention or a service-specific restart cleanup.

Body bytes cannot be released merely because BC04 is terminal. An importer may
still be pending, publication may be unresolved, or the OutputSet may be only
sealed. Existing BC05 staging confirms each referenced artifact through BC06;
the published OutputSet retains those logical artifact references and the exact
receipt binding. The body bytes are therefore no longer required for completed
result replay once BC04 reconciliation and exact BC05 publication are durable,
provided each FetchEvidence maps to its deterministic published BC06 reference.

## Decision

The trusted release operation is composed in
`TrustedAcquisitionApplication`, not in acceptance tooling. It checks exact
BC04 completion, current attempt, generation, finalization reservation and
reconciled receipt; then BC05 published state, manifest and PublicationReceipt;
then every page's deterministic published ArtifactRef and BC06's verified
tenant, source, representation, size and digest. It re-reads BC04 immediately
before release and fails closed for missing or inconsistent owner facts. The
Gateway credential is separate from worker/import credentials. Release is
supported for raw-only v4 and structured JSON when every FetchEvidence page
maps to a published, custody-verified ArtifactRef. Structured HTML and
composite outputs use the same verified page mapping and are included in the
controlled acceptance. Pagination retains bodies if any acquired page lacks
published membership.

The Gateway exposes an owner-local, authenticated body release operation. It
accepts a single FetchEvidence ID and exact tenant, run, attempt, fencing
generation and body digest. The release credential is separate from the
import/read credential and is not provided to workers. The Gateway verifies
metadata identity, body file ownership/type/size and body digest, excludes
active readers under the same mutex used for reader admission, unlinks only the
body file, and synchronizes the directory. Released metadata is written to a
bounded, fsynced archive before body removal and then removed from active entry
accounting. The archive has an independent byte ceiling (default 16 MiB).
Historical metadata remains addressable by exact FetchEvidence ID and counts
against this ceiling; this is bounded archival storage, not unlimited history.
If archive capacity is exhausted, evidence remains active and release fails
closed with `507 evidence_archive_capacity_exhausted`. Repeating a completed
release succeeds idempotently. The authenticated resource snapshot reports
configured archive capacity, used and available bytes, exhaustion state, and
active body/entry usage. Archive accounting includes each encoded metadata
record's newline. Startup reconciles interrupted archive/body/active-metadata
transitions and rebuilds both active and archive accounting. Filesystem and
datastore operations are not globally atomic.

Eligibility is established by the trusted lifecycle caller through public
owner interfaces: BC04 reports `succeeded` with exact current attempt,
generation, finalization reservation and reconciled receipt; BC05 reports the
matching published OutputSet and PublicationReceipt; bounded published reads
contain the deterministic ArtifactRef for each exact FetchEvidence body. BC05
publication already requires successful BC06 custody confirmation for those
references. The Gateway does not query BC04, BC05 or BC06 storage. No worker or
agent receives a release operation or credential.

## Alternatives

1. Retain all Gateway bodies indefinitely. This preserves replay but finite
   byte capacity eventually prevents recurring acquisition.
2. Delete bodies on BC04 terminal state. This is unsafe for pending imports,
   unresolved publication and failed/cancelled work with residual effects.
3. Use a universal TTL or cross-store garbage collector. This adds broad
   retention semantics without owner-local evidence and is outside M9.
4. Release after owner-verified durable publication. This is the smallest
   bounded lifecycle transition that restores byte capacity while preserving
   historical Gateway provenance.

## Consequences and limitations

- Release is per FetchEvidence and explicit. A narrow raw-only operator command
  resolves Gateway metadata and delegates eligibility to the reusable
  application. Structured release is available only while the verified page
  bundle is in the trusted importer. No periodic cleanup service is added.
  The application checks an injected operator allowlist; access to the separate
  Gateway release credential remains the Gateway authorization capability.
- Active body-bearing entries and durable historical metadata are distinct
  capacity dimensions. The bounded archive can fill; operators preserve its
  volume and monitor its capacity. No automatic provenance purge is supported.
- Metadata-only evidence cannot serve historical body retrieval. The retained
  FetchEvidence identity, digest, size, request/response provenance, BC05
  published records and BC06 artifact identity remain resolvable.
- Pending imports, unresolved reservations, failed attempts, cancelled runs
  with unresolved effects, and unpublished or partially staged OutputSets
  retain Gateway bodies.
- The Gateway and datastore operations are not one distributed transaction.
  If unlink succeeds but response acknowledgment is lost, the metadata-only
  state makes retry idempotent and restart reconstructs reclaimed bytes.
- The mechanism does not delete BC05 or BC06 artifacts and does not claim
  universal retention cleanup.
- The current acceptance proves bounded controlled cases. Real-source policy,
  protected A6 operation and production authorization remain separate gates.

## Validation criteria

- Wrong release credentials, tenant, run, attempt, generation or digest fail
  closed without changing files or capacity.
- Active readers block release; concurrent readers and release serialize.
- Repeated release and restart after unlink preserve metadata and report zero
  body-byte usage.
- Capacity exhaustion rejects a new transfer; safe release reclaims real bytes
  and an active entry slot; subsequent transfers succeed without resetting
  persistence. Historical metadata remains addressable after restart.
- BC04 pending/unresolved state and BC05 sealed/unpublished state never invoke
  release. Published output and BC06 artifact remain independently readable.

This decision remains Proposed until human architectural review.
