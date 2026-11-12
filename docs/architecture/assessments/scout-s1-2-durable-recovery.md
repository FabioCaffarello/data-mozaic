# Scout S1.2 — baseline and durable recovery assessment

## Audit baseline

At start, the repository root was `/Volumes/OWC Express 1M2/Develop/data-mozaic`; branch `main` at `7a731a1` (`Merge pull request #1...`), tracking `origin/main`. S0/S1/S1.1 files were already modified or untracked. `git diff --stat` showed 12 tracked modified paths; `git ls-files --others --exclude-standard` listed Scout app, v1 schemas/fixtures, S0/S1.1 docs and tests. No ZIP appeared in the current scan; no cause is inferred. No secrets or large binary artifacts were identified in this baseline list. The branch `feat/scout-s1` did not exist, so it was created without switching away from or discarding the dirty tree.

GitHub CLI was authenticated to `FabioCaffarello`; `origin` points to the repository. Docker CLI exists but `docker info` failed because the Docker daemon socket is unavailable. PostgreSQL client exists; no server was found/confirmed. S1.1 reported 18 passing tests; this was historical context, not presumed validation. The complete current code/contracts/tests were read before edits.

## Naming and migration compatibility

Added [naming conventions](../../development/naming-conventions.md). Existing `ScoutRequest.schema.json`, `EvidenceManifest.schema.json`, `DiscoveryFinding.schema.json`, `PlaybookProposal.schema.json` and schema IDs/fixtures remain untouched. Renaming those would affect contract references and consumers; no rename is proposed. New migration path is `migrations/scout/001-durable-state.sql`; ADR number 008 was available. A focused migration naming check is wired into `make check`.

## Implemented slice and ownership

The PostgreSQL adapter uses psycopg 3, explicit SQL, PostgreSQL `READ COMMITTED`, and structured JSON document version 1. One transaction creates a session plus command receipt. A save transaction performs version CAS, updates receipt snapshot/status, and inserts a stable proposal outbox intent. BC06 continues to own bytes; BC14 stores session evidence references. BC15 owns proposal receipt/approval decisions; outbox transport `sent` and business ACK are distinct. No database/broker imports enter the domain. The offline CLI remains backed by fakes.

## Verification status and limitations

Integration tests are configured for CI PostgreSQL service and cover receipt visibility through a new repository instance, fingerprint conflict, two-connection CAS, and rollback. They were not run locally because Docker daemon is unavailable. `make ci`/`make check`, offline demo, formatter and diff checks are to be recorded in the final execution report. Current tests do not yet prove kill/restart at each external side-effect boundary, durable BC06 uncertain-write reconciliation, relay leasing/backoff, or production egress. ADR 008 remains Proposed; S1 and PR gates remain pending until PostgreSQL CI and the remaining recovery scenarios pass review.
