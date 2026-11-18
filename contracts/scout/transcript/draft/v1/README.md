# Scout transcript draft v1

Owner: BC14. Consumer: BC15's illustrative CapturePlaybook draft generator. Status: **draft / illustrative**, not a public stable contract.

This is a redacted projection of a reauthorized BC14 session. It contains immutable source/session identity, approved scope snapshot, verified evidence metadata, findings, proposal, fixture pages and producer/redaction metadata. It never contains raw page bytes. `integrity.approval_authentication` deliberately states that a local approval reference is not cryptographically signed. The schema validates shape; BC14 validates source/session/evidence ownership and current authorization before export, and BC15 remains responsible for draft validation/replay.

Canonical serialization is UTF-8 JSON with sorted object keys, no insignificant whitespace, and a trailing newline. The SHA-256 is returned beside the transcript bytes and covers those exact bytes. The transcript is bound to the exported aggregate version; this local format does not provide a signature or claim independent approval.

The draft is versioned independently from `contracts/scout/v1` and `contracts/capture-playbook/draft/v1`. Changes require consumer tests and a new draft version if incompatible.
