# FetchEvidence draft/v1

Owner: acquisition gateway integration (technical evidence contract; it does not transfer BC01, BC11, BC04 or BC06 ownership).

The gateway produces this immutable evidence after an actual bounded HTTP response has been read and its body digest verified. `acquired_at` is the acquisition timestamp, not a source reference date, ingestion time, or publication time. `body_digest` and `body_size` describe the decoded HTTP entity body, not original wire bytes. `body_ref` is a logical gateway-owned spool reference and is never interpreted as a worker-selected filesystem path.

The worker's response headers and result files are declarations only. Trusted import must retrieve the gateway-owned metadata and bytes, verify execution identity and digest, and then transfer selected bytes through BC06. This draft does not define that importer protocol or authorize publication.

The contract intentionally omits request headers, cookies, credentials and response headers. Its URL fields are origins only; request paths and full target values are represented by digests, and query names are retained without query values.
