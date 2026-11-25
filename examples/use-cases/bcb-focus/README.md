# Banco Central Focus — proposed reference case

**Status: draft; documentation-derived; not an approved source revision, policy, playbook, or acquisition.** Checked against official public documentation on 2026-11-25. No Focus endpoint was called and no Focus record was acquired in this increment.

## Documented facts

- The BCB Open Data Portal identifies BCB/Dstat as the dataset maintainer and lists the Expectativas de Mercado dataset under ODbL. The dataset description says it contains statistics calculated from participant expectations and identifies Sistema Expectativas de Mercado (EXP) as the origin.
- The portal exposes an OData resource, API data browser, Swagger documentation, consolidated HTML resources, and JSON resources for monthly, Selic, quarterly, annual and Top 5 aggregates. It marks the institution-level JSON resource disabled due to confidentiality risk. This reference case must not attempt to access that disabled resource.
- The BCB describes Focus as market expectations, not BCB forecasts. The weekly Focus report is normally released on the first business day of the week; its reference/cutoff and publication timestamps are distinct.
- Official documentation provides public statistical history and describes report publication timing. It does not provide a platform authorization, quota, or permission for this repository to execute acquisition.

## Candidate, unverified acquisition shape

Start with one documented aggregate JSON resource from the public catalog, selected only after a separately approved Scout exploration. A candidate pattern is one bounded GET to the official Olinda service, optionally followed by bounded OData pagination if the exact resource documentation requires it. Treat every entity name, query parameter, field, ordering, page limit, date meaning, and response schema as unknown until the exact API resource documentation and authorized evidence are reviewed.

The likely observation identity should include the selected resource, source-provided reference date, and an acquisition/run observation identity. Payload digest alone must not collapse repeated observations. Candidate source natural keys and missing-value semantics remain unknown.

## Preliminary contract requirements

- Keep the complete bounded response body as declared decoded HTTP entity bytes where the evidence policy requires it; do not label it wire bytes.
- Preserve response acquisition time separately from BCB reference date, ingestion time, and publication time.
- Store the official resource identifier, requested/effective URL, status, media type, and content digest after sanitizing credentials and sensitive headers.
- Validate and version the record payload only after authorized exploration has supplied independent captured fixtures.
- Do not use institution-level data or any disabled resource.

## Proposed narrow Scout exploration scope (human approval required)

After approval, inspect only the official dataset page, its linked Swagger/OData documentation, and one selected aggregate public resource for one bounded request. Record the selected resource's documentation, request parameters, response headers/body, license/usage terms, observed rate/size behavior, and reference-date semantics. Do not access institution-level endpoints, authenticate as a participant, enumerate unrelated resources, or run repeated polling. The allowed origin and request count/byte limits must be filled from the approved policy before Scout runs.

See the separately bounded [documentation-only Scout scope proposal](exploration-scope-proposal.md). It deliberately does not authorize a request for dataset records.

## Official sources

- [BCB Open Data: Expectativas de Mercado dataset](https://dadosabertos.bcb.gov.br/dataset/expectativas-mercado) — maintainer, dataset description, ODbL declaration and resource catalog.
- [BCB: Relatório Focus](https://www.bcb.gov.br/controleinflacao/relatoriofocus) — report description, market-expectation framing and publication cadence.
- [Official Olinda Swagger UI](https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/swagger-ui3) — linked by the dataset catalog; content was not retrieved during this review due a documentation endpoint timeout.
- [Official Olinda OData endpoint](https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/) — linked by the dataset catalog; direct content is XML and was not interpreted as data or called for records.
