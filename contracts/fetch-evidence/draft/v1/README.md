# FetchEvidence draft/v1

Owner: acquisition gateway integration (technical evidence contract; it does not transfer BC01, BC11, BC04 or BC06 ownership).

The gateway produces this immutable evidence after an actual bounded HTTP response has been read and its body digest verified. `acquired_at` is the acquisition timestamp, not a source reference date, ingestion time, or publication time. `body_digest` and `body_size` describe the decoded HTTP entity body, not original wire bytes. `body_ref` is a logical gateway-owned spool reference and is never interpreted as a worker-selected filesystem path.

The worker's response headers and result files are declarations only. Trusted import must retrieve the gateway-owned metadata and bytes, verify execution identity and digest, and then transfer selected bytes through BC06. This draft does not define that importer protocol or authorize publication.

The contract intentionally omits request headers, cookies, credentials and response headers. Its URL fields are origins only; request paths and full target values are represented by digests, and query names are retained without query values.

## Trusted body release operation (draft)

The Gateway owner exposes `DELETE /internal/v1/evidence/{fetch_id}` only to a
trusted lifecycle operator holding the distinct
`GATEWAY_EVIDENCE_RELEASE_TOKEN`. The worker/agent receives neither this
credential nor this operation. Requests are bound by `X-DM-Tenant-ID`,
`X-DM-Run-ID`, `X-DM-Attempt-ID`, `X-DM-Generation`, and
`X-DM-Body-SHA256`. Fetch ID must be exactly 32 lowercase hexadecimal
characters. Missing or mismatched scope, a missing metadata/body identity, or
an active evidence reader fails closed. The request contains no caller-supplied
`completed` flag.

The trusted caller must first establish the durable release facts through
owner-published interfaces: BC04 `succeeded` for the exact attempt/generation
with reconciled finalization reservation and receipt; BC05's exact OutputSet
is `published` with a matching PublicationReceipt; and bounded published
membership contains the deterministic BC06 ArtifactRef matching each released
FetchEvidence digest and size. BC05 staging confirms BC06 custody before
publication. The Gateway itself checks its own exact identity/body digest and
reader state; it does not query another context's persistence.

Success returns JSON with `contract_version: fetch-evidence/v1`, the exact
`fetch_id`, `body_state: released`, and immutable `body_digest`. A repeated
successful release returns the same state. It removes only the Gateway body;
metadata/provenance moves to the bounded Gateway archive and no longer consumes
an active evidence entry. Archive bytes remain finite and accounted. BC06
ArtifactID retrieval remains the historical byte-read path after publication.
If cross-owner eligibility cannot be established or the archive is full,
callers retain the active evidence body and metadata.
The response shape is defined in
[`EvidenceBodyReleaseResponse.schema.json`](EvidenceBodyReleaseResponse.schema.json).

This operation remains draft and is exercised by controlled Gateway tests, the
reusable trusted acquisition application, and the raw-only operator command; it
is not approval for a production source or an authorization policy.
