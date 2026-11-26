# Immutable payload contract installation

Status: **draft / controlled installation**. BC08 owns exact payload contract
identity and revision semantics. The registry manifest and schema documents
are installed as reviewed repository artifacts; they are not mutable runtime
configuration and this directory does not provide a network registration API.

The `digest` is `sha256:` plus the lowercase SHA-256 of the exact UTF-8 schema
file bytes. Payload JSON is parsed strictly by the result-bundle reader before
JSON Schema validation. The initial resolver supports only JSON Schema Draft
2020-12, schemas up to 64,000 bytes, self-contained schemas, and no `$ref`
resolution. It never retrieves external resources. A new content revision
uses a new immutable identity/version entry; changing bytes under the same
identity and version is a conflict.

The registry installation proves which bytes are present, not who approved
them. BC15 release approval binds the exact `contract_id`, `version`, and
digest. BC04 copies that binding into its durable execution binding. Workers
cannot select or register contracts. New payload contract additions require a
reviewed repository change and the owner-approved release path.

The included synthetic and controlled-source schemas are acceptance fixtures,
not authorization for real source acquisition. Approval and eligibility
workflows remain part of the later Source and Release lifecycle increment.
