# BotRelease draft v2

Owner: BC15 Collector Engineering. Status: draft / illustrative; not an
operational approval service or production authorization by itself.

BotRelease v2 adds a required `payload_contract` reference containing the exact
`contract_id`, `version`, and `sha256:` digest of the immutable BC08 schema
installation. Its release digest covers this reference. The trusted signed
authority snapshot is still required for approval and BC04 copies the reference
into the durable execution binding.

The v1 schema remains unchanged for historical document interpretation. BC04
does not use v1 for new contract-bound admission; old runs without a reference
are not silently assigned a schema. The compiler/runtime and BotBuild formats
are unchanged by this wire contract revision.
