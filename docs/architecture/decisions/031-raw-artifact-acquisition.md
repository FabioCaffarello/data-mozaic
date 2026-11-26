# ADR 031: Raw artifact acquisition through the existing output and custody owners

- Status: Proposed
- Date: 2026-11-26
- Owners: BC15 Collector Engineering, BC04 Execution Coordination, BC05 Acquisition Output, BC06 Artifact Custody

## Context

The platform already owns the required publication primitives: BC05 `ArtifactOutput` publishes logical artifact references and BC06 verifies and stores bounded bytes. The live Playbook profile still requires record selection, field extraction and `RecordOutput`, so those primitives cannot yet be reached by a raw-only acquisition. BotRelease v3 also binds a payload contract and an Input Contract; changing v3 semantics would reinterpret the M5 capability.

## Decision

Add a separately versioned raw-only Playbook and release profile. Its compiled graph contains exactly one authorized GET fetch and one artifact output sink. It has no selectors, record extraction, continuation, redirect, pagination or query parameters. The Playbook's artifact binding declares accepted media types and a maximum entity size. BotRelease v4 binds that output mode and exact artifact constraints. BC13 continues to bind an immutable Input Contract; parameterless work uses the installed empty-object contract and the exact unparameterized GET target. Existing Playbook, BotBuild and BotRelease versions retain their existing interpretation.

The gateway remains the authority for live HTTP evidence. It captures the bounded, identity-content-encoding HTTP response body and records it as `decoded-http-entity`; it does not preserve TLS wire bytes. The artifact is the exact byte sequence in that body. The acquisition application independently retrieves gateway evidence, verifies tenant/run/attempt/generation/source/policy/release/build/fetch identity, representation, size and SHA-256, and sends those verified bytes through BC06's public custody API. It stages a BC05 `ArtifactOutput` referring to the verified BC06 receipt, seals the OutputSet and uses BC04's existing finalization reservation and outcome flow.

The approved release and BC04 admission snapshot bind the output modality and artifact constraints. The worker result is a bounded intent/reference report only; it cannot select validation mode or assert authoritative bytes, digest, media type, provenance or custody. Worker execution remains protected and gateway-only.

## Ownership and dependencies

- BC15 owns the declarative acquisition graph, typed plan and immutable BotBuild.
- BC13 owns Input, Submission and Dispatch intent.
- BC04 owns CaptureRun, attempts, fencing, finalization and terminal state.
- The gateway owns authorized transport and live FetchEvidence.
- The trusted acquisition application verifies evidence and coordinates BC05/BC06 public ports.
- BC06 owns artifact identity and byte custody.
- BC05 owns ArtifactOutput, OutputSet and publication.

No new service or generic storage abstraction is introduced. Existing owner-local HTTP adapters remain in place; current consumers have different authorization and operation semantics, so a shared SDK is not justified by this increment.

## Bounds and compatibility

The acquisition path is limited by the smallest active bound: live Scrapy/gateway response body 2,000,000 bytes, BC04 aggregate response budget, result bundle bound, and BC06 upload bound 2,000,000 bytes. HTTP non-identity `Content-Encoding` remains rejected; ZIP or other artifact formats are stored as-is and never unpacked. Content-Type is policy metadata, not proof of document validity. Source-declared filenames are not used as paths or object keys.

Historical record profiles and their payload-contract requirements remain unchanged. Parameterized v3 continues to authorize one initial request and one scalar query parameter. Raw-only v4 has no parameterized continuation behavior. Runtime identities remain exact; new compiled runtime bundle digests are distinct build identities.

## Consequences

Raw artifacts can be independently consumed after publication without rerunning the bot. The worker does not retain the response body in its result bundle; the gateway owns the authoritative spool. The gateway transport, Scrapy response handler, trusted evidence client and BC06 transfer still materialize bounded response bytes at their process boundaries, and the current HTTP upload sends a bounded in-memory body. This is not end-to-end streaming. The controlled acceptance covers PDF, CSV, ZIP, non-UTF-8 binary and raw JSON through actual source transport, evidence, custody, publication and independent retrieval. Streaming, larger object support, multipart transfer and orphan-custody reconciliation remain M7 work.

## Rejected alternatives

- Reusing `RecordOutput` with an empty or synthetic payload misstates the observation modality.
- Treating `output.raw/v1` reference metadata as final custody would omit BC06 artifact identity and BC05 publication.
- Making payload contracts optional inside BotRelease v3 would reinterpret an existing contract version.
- Parsing or unpacking content would change the requested raw artifact semantics.
- A new artifact service, storage provider or cross-context generic byte SDK is unnecessary for the current concrete consumers.
