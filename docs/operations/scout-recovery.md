# Scout recovery operations (local S1)

This runbook is for the local PostgreSQL S1 profile. The BC06 and BC15 collaborators in tests are doubles. No production dispatcher, BC06 inventory client, or artifact deletion path exists.

## Start and stop the local database

```sh
cp .env.example .env   # only if no local .env exists; review local-only values
make db-up
make db-status
make db-migrate
make db-down            # stops the container; preserves the named volume
```

Do not use `docker compose down -v`. The `.env` file is ignored and must not be copied into tickets or logs.

## List and investigate outbox items

Load the local connection only in the current shell, then query metadata. Do not select or print `payload`, `transport_receipt`, or `business_ack` in shared terminal output.

```sh
set -a; . ./.env; set +a
psql "$SCOUT_DATABASE_URL" -c "SELECT event_id, publication_state, attempt_count, next_attempt_at, lease_owner, lease_deadline, fencing_token, error_class, last_result FROM scout_outbox ORDER BY created_at"
```

`pending`, `leased`, `retry_scheduled`, `sent`, and `dead_letter` are BC14 relay facts. `sent` means receiver receipt only. It does not mean playbook approval. A dead letter is retained; this increment does not implement automatic redrive. Investigate the error class and stable event ID with the BC15 owner before any authorized administrative action.

## Reconcile custody

BC14 can reconcile a persisted intent by calling `ScoutApplication.reconcile_custody_intent(artifact_id)` through an application composition that supplies a BC06 lookup adapter. The lookup returns identity, session, source revision, size, digest and normalized custody status; BC14 checks those against the durable intent and asks the adapter to verify integrity. `confirmed` means all checks passed; `not_found` permits policy-controlled retry; `manual_review` leaves the intent unapproved. The repository does not provide a productive BC06 adapter or a standalone operator CLI yet.

BC06 owns global inventory and retention. A candidate absent from BC14 must be reported by a BC06-owned inventory collaboration and recorded as a manual-review diagnostic. Inspect metadata without reading bytes:

```sh
psql "$SCOUT_DATABASE_URL" -c "SELECT review_id, artifact_id, reported_by, status, diagnostic, created_at FROM scout_orphan_reviews ORDER BY created_at"
psql "$SCOUT_DATABASE_URL" -c "SELECT event_id, artifact_id, event_kind, owner, details, occurred_at FROM scout_reconciliation_events ORDER BY occurred_at"
```

No artifact is deleted by Scout. Retention, quarantine and any removal require the BC06 owner and applicable policy approval. Never infer global orphan absence from BC14 tables.

## Run the independent relay worker

The application only persists the outbox intent and returns with `delivery_pending`. A separate process runs `scout.relay_worker`, obtains the repository connection from `.env`, and loads a BC15 adapter factory specified as `module:callable`. No production BC15 adapter is supplied in S1; use a test-only adapter for local rehearsal. Example controlled one-shot invocation:

```sh
set -a; . ./.env; set +a
export SCOUT_BC15_RECEIVER_FACTORY='tests.integration.test_postgres_scout:build_test_receiver'
make scout-relay-once
```

Use `make scout-relay` for the polling worker. Ctrl-C or SIGTERM stops new claims after the in-flight operation returns. `make scout-relay-once` claims at most one event. Lease expiry permits recovery after a crash. A stale worker's fence cannot persist an acknowledgement. The receiver must deduplicate the stable message ID because an effect may be delivered more than once. `sent` means `received_by_bc15`; only a later authorized application replay marks the session complete. There is no `playbook_approved` state in BC14.

To reproduce crash recovery, retry, receiver deduplication and dead-letter against the PostgreSQL test profile:

```sh
set -a; . ./.env; set +a
SCOUT_TEST_DATABASE_URL="$SCOUT_DATABASE_URL" make test-recovery
```
