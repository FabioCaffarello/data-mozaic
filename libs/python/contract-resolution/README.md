# mozaic-contract-resolution

BC08-owned internal Python package for resolving exact immutable raw-payload
JSON Schema revisions from the repository-installed contract catalog.

## API

- `ContractReference(contract_id, version, digest)` is the exact immutable
  identity (`sha256:` + lowercase SHA-256 of exact UTF-8 schema bytes).
- `ImmutableContractRegistry(contracts_root).resolve(reference)` returns a
  verified `ResolvedJSONSchema` or raises `ContractResolutionError`.
- `ResolvedJSONSchema.validate(payload)` validates without including rejected
  payload values in its diagnostic.
- `installed_reference(id, version)` reads an installed reference for trusted
  release composition. It establishes installation identity only; it does not
  authorize or approve a release.

## Consumer and dependencies

Current consumer: `apps/acquisition-application`, whose validation adapter
implements the existing BC05 payload-validation port. BC04 execution and BC15
BotRelease retain their own contract projections and authority; this package
does not import either context. Runtime dependency: `jsonschema`.

## Limits and exclusions

Only Draft 2020-12 self-contained schemas are supported. Registry document limit
is 256,000 bytes; schema limit is 64,000 bytes. Duplicate JSON keys, non-finite
numbers, symlinks, unsafe paths, digest divergence, invalid schemas and any
`$ref` fail closed. There is no network retrieval, mutable registration API,
approval or eligibility workflow, persistence, source-specific business rule,
compatibility inference or deletion. A schema reference is not authorization.

The package is internal `0.x` with no external compatibility promise. Run
`make test-libraries` for independent tests and the fixture/live acquisition
targets for consumer conformance. A future consumer must retain exact identity,
digest, bounds and fail-closed diagnostics.
