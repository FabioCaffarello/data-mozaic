# BotRelease draft v3

Owner: BC15 Collector Engineering. Status: draft / illustrative; this is not
an operational approval service.

V3 preserves every v2 field and adds a required exact immutable
`input_contract` reference alongside `payload_contract`. The release digest
covers both bindings. A v2 release remains interpretable as its historical
payload-only form but cannot admit Work Intake submissions that require input
parameters. New execution admission never assigns an input contract to a v2
release.

The contract reference uses the BC08 identity, version, and SHA-256 digest of
exact UTF-8 schema bytes. Input parameter validation is separate from raw output
payload validation. A schema reference does not grant network authority; BC11
policy and the gateway continue to enforce the request scope.
