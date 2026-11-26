# BotRelease draft v4

BotRelease v4 approves one raw-only `ArtifactOutput` profile. `output_contract` binds an explicit media type allowlist and maximum entity bytes; there is no payload contract because no records are emitted. `input_contract` remains an exact immutable contract for the logical Input. A parameterless artifact request uses an empty-object Input Contract and a `GET` request with no query, keeping BC13's durable Input identity and submission semantics.

The release is bound to an exact BotBuild v4, runtime digest, SourceRevision and AcquisitionPolicy. It does not authorize parsing, unpacking, pagination, redirects or parameterized continuation. The Playbook and BotBuild carry the same artifact bounds; BC04 snapshots them at admission for trusted import.
