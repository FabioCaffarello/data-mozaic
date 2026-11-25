# ExecutionCapability draft v1

- Owner: BC04 Execution Coordination.
- Consumer: the trusted acquisition gateway.
- Compatibility: this internal draft is consumed by the controlled compiled Scrapy live smoke; it does not yet establish durable BC04 admission for that smoke.
- Purpose: authorize bounded requests for one admitted tenant/run/attempt/generation and bind them to the approved source revision, policy digest, release, verified build, runtime profile, recipient audience and execution deadline.
- Authenticity: BC04 signs RFC 8785 canonical JSON claims with Ed25519. The `signature` member is excluded from the signed claims. Gateway receives public verification keys only; the worker receives the signed capability, never signing material.
- Lifetime: at most two minutes and further capped by the BC04 attempt lease and execution deadline. A renewal requires a new capability. Expired capabilities cannot authorize new requests.
- Revocation and replay: the durable authority registry prevents new admissions from stale snapshots. A current-attempt operation authorization channel from BC04 to the gateway is still required to make cancellation or release revocation block each future request immediately. The capability may authorize multiple in-scope fetches until expiry; quotas count every request. A gateway restart invalidates capabilities issued before gateway process start and requires governed recovery rather than resetting their budgets. No claim is made that revocation cancels a request already admitted or sent.
- Finalization: independent of BC04's durable, non-revocable-until-resolved finalization reservation and publication grant.
- Status: draft/illustrative; not an approval or release.
- The cross-language signature vector is at `examples/conformance-vector.json`. Its public key is test-only.
