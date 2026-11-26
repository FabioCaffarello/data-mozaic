# ADR 027 — Trusted Acquisition Application composition boundary

- Status: Proposed
- Date: 2026-11-25
- Owners: BC04 Execution Coordination, BC05 Acquisition, BC06 Artifact Custody, BC15 Collector Engineering
- Scope: in-process validation and import of protected fixture/live Scrapy result bundles into the existing custody, OutputSet, and finalization protocols

## Context

Production-relevant result validation, evidence resolution, custody coordination, output construction, staging and recovery composition had accumulated in root tooling. The live importer also imported private fixture-import functions and types. No existing BC exclusively owns this entire workflow: BC15 owns the runtime result semantics, BC04 owns attempt validity and finalization, BC05 owns CaptureOutputs and OutputSets, BC06 owns artifact custody, and the gateway owns authoritative live evidence.

## Decision

Establish `apps/acquisition-application/` as a narrow in-process application composition boundary. It owns orchestration and application-level validation only. It is not a new bounded context or deployable. Its consumer-owned ports and adapters delegate to owner interfaces and public BC06 contracts.

The application verifies an exact BotBuild and its declarative plan, checks fixture or gateway evidence using profile-specific trust adapters, re-evaluates result semantics from trusted bytes, and coordinates BC06 custody plus BC05 stage/seal. It delegates publication authorization, reservation, receipt reconciliation and terminal completion to the existing BC04 protocol. Completed-result replay is read-only and must match both the BC04 reservation/receipt and the already published BC05 OutputSet.

Controlled expected records, fixtures, generated authorities/keys, scenario setup and fault injection remain in harnesses. Production checks derive from trusted execution context, the compiled plan, owner-bound contracts and authoritative evidence.

## Alternatives

- **BC04-owned importer:** rejected because BC04 does not own evidence interpretation, artifact custody or BC05 output construction; this would couple execution coordination to capture semantics.
- **BC05-owned importer:** rejected because BC05 does not own Build/runtime interpretation, worker result validation or attempt authorization.
- **BC15-owned importer:** rejected as the sole boundary because BC15 does not own execution validity, custody, OutputSet persistence or publication.
- **New bounded context/service:** rejected because the workflow composes existing owner capabilities and has no independent state/lifecycle or operational deployment requirement.
- **Keep importer in tooling:** rejected because production validation and idempotent owner coordination need a reusable application owner independent of test scenarios.

## Consequences and constraints

- BC04, BC05, BC06, BC15 and the gateway retain their domain state, policies, migrations, and authoritative decisions.
- The application does not own provider persistence, signing keys, retries, scheduling or a second finalization protocol.
- Fixture and live evidence share import orchestration but retain separate authority adapters.
- Contract resolution is intentionally limited to immutable current contract material. A release-bound contract resolver remains an M3 increment.
- The first BC06 HTTP adapter is application-specific, not a general SDK; a broader SDK requires another compatible consumer and stable contract evidence.
- The bundle reader remains application-owned; it is not a generic library because no independent producer/consumer API has been established.
- Bounded bodies and result sets are still materialized. Existing bounds remain mandatory; incremental streaming is deferred to measured M7 work.
- Pre-reservation recovery does not gain durable import state. Partially staged sets remain invisible to published-only consumers and are handled through existing owner semantics.

## Validation

Keep application unit/contract tests, fixture and controlled-live integrated scenarios, replay, lost-ack reconciliation, and negative publication assertions. ADR status remains Proposed until human approval; implementation and green checks do not accept this decision.
