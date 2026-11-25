# Proposed Scout exploration authorization — BCB Focus documentation

**Approval state: proposed, not authorized.** No Scout or Scrapy calls were made under this proposal.

## Purpose

Resolve the Open Data Portal's API documentation and identify one public aggregate JSON resource suitable for a later separately reviewed source-acquisition request. The current dossier does not establish entity names, parameters, schema, source quotas, or permission to make data requests.

## Proposed first session scope

- Source owner: Banco Central do Brasil / BCB-Dstat, as shown in the official catalog.
- Documentation origin allowlist: `https://dadosabertos.bcb.gov.br` and `https://olinda.bcb.gov.br`.
- Methods: `GET` only.
- Request budget: at most 3 HTTP requests, no automatic link following, 1 MiB per response, 2 MiB total, 60 seconds total.
- Destination rules: public DNS addresses only; reject loopback, private, link-local, multicast, unspecified, and metadata destinations; TLS verification required; deny redirects unless each hop is separately reauthorized under the same origins and budget.
- Scope: the dataset metadata page and linked Swagger/API documentation only. Do not call a data-returning OData entity in this session.
- Exclusions: institution-level data, the catalog resource marked disabled, participant credentials, authentication, POST, arbitrary query exploration, high-frequency polling, unrelated BCB datasets, and external origins.
- Evidence: retain only the bounded documentation response bodies, sanitized response metadata, URL, timestamp, status, and SHA-256 digest under the approved evidence-retention policy.

## Follow-up authorization boundary

After documentation identifies an exact aggregate resource, pause and request a new human review containing its exact path, documented parameters, expected format, access terms, rate/size behavior, observation/reference date semantics, request count, byte limits, and retention. Do not infer that approval of this documentation-only session authorizes fetching dataset records.
