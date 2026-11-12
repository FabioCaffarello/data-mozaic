# Scout S1.2b — Docker and recovery gate assessment

## Baseline and audit

The worktree was on `feat/scout-s1`; the existing staged and unstaged S0–S1.2 changes were preserved. There was no Compose file or `deployments/compose` / `infra/compose` convention. Docker Engine 28.5.1 and Compose 2.40.3 were available; the daemon was running. Prior PostgreSQL integration tests had proved receipt visibility after creating a new repository object, changed-fingerprint conflict, two-connection CAS, a manually injected rollback, and outbox persistence. They shared the supplied test database and did not run a process crash or actual Compose volume cycle.

## Work performed

Added one local Compose profile at `deployments/compose/postgres.compose.yaml`: official `postgres:18.4`, explicit `data-mozaic-scout-local` project, loopback-only configurable host port defaulting to 55432, `pg_isready` healthcheck, named volume mounted at `/var/lib/postgresql`, and 30-second stop grace. `.env.example` contains a clearly local-only placeholder; `.env` remains ignored. The Scout CLI still runs on the host.

Image inspection on the local Docker `linux/arm64` daemon reported RepoDigest `postgres@sha256:a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636`. Compose and CI pin the human-readable patch tag `18.4`; the inspected digest is recorded as local arm64 evidence, not used as a cross-architecture image pin.

Migrations remain explicit SQL at `migrations/scout/001-durable-state.sql`; no copy was made. The psycopg runner uses a PostgreSQL transaction advisory lock and SHA-256 per migration. Existing legacy migration entries without checksums fail closed and require explicit operator reconciliation. The integration harness accepts loopback only, creates a randomized database, and drops only that generated database.

## Recovery evidence and gaps

| Scenario | Status |
|---|---|
| Receipt after new repository instance; same/different fingerprint | PostgreSQL test passes; repository-instance restart, not OS process restart of application. |
| CAS from two independent connections | PostgreSQL test passes; one version writer wins. |
| Transaction rollback | PostgreSQL tests pass for aggregate + receipt and injected failure during save; session CAS, receipt snapshot, and outbox insertion all roll back together. |
| Migration repeat/checksum/data preservation | Test passes against PostgreSQL 18.4; second run preserves marker data, checksum mismatch is rejected, and migration lock is transactional. |
| Child process crash before/after database commit | Test passes: uncommitted row is absent after child exit; committed row survives. This tests transaction durability at the real PostgreSQL process boundary. |
| Compose readiness and named-volume persistence over down/up | `docker compose config -q`, `make db-up`, healthcheck, `make db-down`, then `make db-up` all pass; a marker row survives. The real volume is preserved and no `down -v` ran. |
| BC15 lost ACK across process restart | Test-only receiver table commits the proposal receipt, child exits before local confirmation, and a restarted relay finds the same receipt; repeated delivery ID leaves one logical receiver row. `sent` and business receipt remain separate; this is a test double, not BC15 production. |
| BC06 unknown outcome and digest reconciliation | Stable BC14 custody intent persists before effect. Test-only external object table commits bytes then child exits before ACK; restarted test verifies bytes/digest and marks intent verified. No orphan retention/deletion policy or productive BC06 client exists. |
| Complete command replay with authorization after new process | Not exercised end-to-end through the application/BC11 adapter. |

## Gate

The local Compose profile, migration checksum/lock, test database isolation, process-level transaction crash test, outbox rollback, and BC15/BC06 receiver/custody simulations improve reproducibility and evidence. S1 remains **BLOCKED / NO-GO for PR**: full `ScoutApplication` process restart/replay with fresh authorization is not tested; relay reclaim/lease/backoff is not implemented; orphan retention policy is pending; and Proposed ADRs still need human architecture review. The BC06/BC15 tests use deterministic test doubles and do not prove production services.

## Commands executed in S1.2b

- `docker compose --env-file .env -f deployments/compose/postgres.compose.yaml config -q`: passed.
- `make db-up` / `make db-status`: passed; service reported `healthy` and published only `127.0.0.1:55432`.
- `make db-migrate` twice: both reported current; marker-data preservation and checksum-negative behavior passed in integration tests.
- `make test-integration` and `make test-recovery`: 9 tests passed each against the Compose PostgreSQL 18.4 database; each harness-created database was dropped afterward.
- A marker row survived `make db-down` followed by `make db-up`; no volume removal command was run.
- `make ci` with `SCOUT_TEST_DATABASE_URL` loaded from ignored local `.env`: 27 tests passed, zero skipped/failed.
- `make check` with the same test DSN: 27 tests passed, zero skipped/failed. Without a test DSN, 18 tests pass and 9 PostgreSQL tests are visibly skipped; that run is not the S1 gate.
- Offline demo passed. `git diff --check` and `git diff --cached --check` passed.
