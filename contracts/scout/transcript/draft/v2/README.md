# Scout transcript draft v2

Owner: BC14. Consumer: BC15. Status: draft / illustrative; not a public Scout v1 wire contract.

Version 2 makes investigation navigation explicit for offline playbook authoring. It records the requested URL and effective URL for the initial observation, the deterministic observation sequence, observation identities, request counts, evidence references, and observed discovery relations. It remains metadata-only and does not contain raw response bytes. BC14 reauthorizes and verifies custody before export; BC15 independently validates the DTO and refuses incomplete or inconsistent navigation.

BC15 can inspect transcript draft v1 but does not generate a CapturePlaybook from it: v1 has no unambiguous start URL or persisted exploration order. This is a deliberate typed incompatibility, not a silent fallback to `pages[0]`. Session payload hydration remains backward compatible because the new aggregate fields are optional; legacy HTTP observations lacking sequence are ineligible for v2 authoring until recaptured through the current use case.

CapturePlaybook `draft/v1` remains unchanged. Its `output.raw` preserves captured response bodies only; it does not extract structured records. Structural validation, replay execution, and acquisition acceptance are separate. The acceptance oracle is operator-authored outside the transcript and generated draft.

Canonical JSON and SHA-256 behavior are the same as transcript draft v1. Raw bytes remain in BC06 custody or the separately exported private replay fixtures, not this contract.
