# ADR 032 — bounded raw artifact transfer

**Status: Proposed**

## Context

M6 raw-only v4 persisted complete gateway evidence, but gateway acquisition,
worker delivery, evidence retrieval, trusted import and BC06 upload each
materialized body bytes. The live Scrapy handler also used one asynchronous
read, which could return a partial body even when the gateway had completed a
larger response. The existing v4 immutable release/build capability is capped
at 2,000,000 decimal bytes. BC06's local provider accepts up to 100 MiB per
upload, but that does not authorize a larger acquisition profile.

## Decision

For the raw-only result, the worker asks the gateway to acquire and persist the
complete authorized response, then receives only bounded evidence metadata and
the evidence reference. The worker still executes the immutable build and its
normal Scrapy lifecycle. The trusted importer independently resolves the
evidence with exact tenant/run/attempt/generation binding, writes it to a
private temporary file while hashing and counting bytes, and streams that file
with an explicit Content-Length through the existing public BC06 HTTP
contract. BC06 independently spools and verifies the upload before object
commit. BC05 publication and BC04 finalization remain unchanged.

Gateway upstream and downstream body copies use disk-backed files and fixed
64 KiB transfer buffers. Evidence metadata becomes visible only after the
complete body is bounded, hashed, fsynced and committed. Evidence reads open
one validated inode, hash it incrementally, rewind that same descriptor, and
transfer from it. Gateway spool capacity is reserved under the local process
mutex before fetch; reservations have unique identities and release once.
Restart rebuilds usage from paired evidence files and removes only incomplete
temporary or unpaired files. This quota is single-instance, not distributed.

The raw-only Scrapy transport uses reference-only mode. Structured profiles
continue to receive the complete bounded response because their extraction
semantics consume it. The live runtime identity advances to
`scrapy-live-http-runtime/1.3.0`; Playbook v4, BotBuild v4, BotRelease v4,
FetchEvidence v1 and existing BC04/BC05/BC06 contracts retain their meaning.
The v4 artifact limit remains 2,000,000 bytes.

## Alternatives considered

- **Keep bounded full-body materialization:** rejected for raw-only because
  the worker and importer did not need the bytes as memory objects.
- **Stream source directly to BC06:** rejected because it would remove the
  gateway's authoritative, recoverable evidence boundary and couple acquisition
  authorization to custody.
- **Multipart object upload:** rejected because the existing BC06 single-part
  path accepts the current authorized envelope and already verifies a bounded
  private spool.
- **Universal stream envelope or extracted client SDK:** rejected because the
  gateway evidence, BC06 custody and BC05 output lifecycles are distinct, and
  only one compatible raw upload consumer exists.
- **Raise the v4 or provider limits:** rejected. An increased capability needs
  an explicitly versioned and approved release/build contract, separate from
  implementation buffer limits.

## Resource and recovery bounds

The controlled gateway uses a 64 MiB persistent evidence spool, at most 500
evidence entries, and at most eight active fetches. The raw runtime and v4
profile cap each artifact at 2,000,000 bytes. BC06 local upload is configured
for 104,857,600 bytes, four concurrent uploads and a 440,401,920-byte temporary
budget; startup verifies aggregate maximum spool requirements. These provider
bounds do not enlarge v4 authorization. The importer temporary copy is capped
by the same admitted response/artifact limits and is removed after custody and
staging complete or fail.

Completed gateway evidence is retained. Current contracts do not provide a
durable cross-context proof that every pending import, custody operation,
staging attempt and reserved publication has finished, so automatic evidence
TTL or deletion is unsafe. Capacity exhaustion is explicit and fails closed.
The authenticated gateway resource snapshot exposes only component-wide
capacity counters, with no tenant or artifact identifiers. Custodied but
unpublished artifacts remain BC06-owned residuals; no cross-store deletion or
garbage collector is introduced.

## Evidence

See [M7 resource maturity implementation evidence](../assessments/acquisition-resource-maturity-2026-11-27.md)
for the byte-flow inventory, baseline and post-change controlled measurements,
integrated 64 KiB/1,000,000/2,000,000-byte workloads, overlap and capacity
tests, compatibility and remaining limits.
