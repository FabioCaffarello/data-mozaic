# CapturePlaybook draft v4

Draft BC15 acquisition playbook with an explicit raw artifact profile. A raw-only v4 graph contains exactly one `http.fetch/v1` and one `output.artifact/v1` directly bound to that fetch. It has no record selector, extraction, pagination, redirect, or Input parameter operation.

`output_contract` is an artifact constraint with an explicit allowlist of media types and a maximum body size. The fetch's declared media type must be in that allowlist. The gateway remains authoritative for the actual response media metadata and bytes; the media type is not proof of file semantics. The raw output is the complete bounded decoded HTTP entity body (`decoded-http-entity`), with HTTP non-identity Content-Encoding rejected. Artifact contents such as ZIP files are not unpacked.

This profile compiles to `collector-ir/v3` and `bot-build-manifest/v4`. Historical Playbook v1–v3 and BotBuild v1–v3 retain their prior semantics.
