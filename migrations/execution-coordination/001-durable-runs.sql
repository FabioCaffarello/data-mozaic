CREATE SCHEMA IF NOT EXISTS execution_coordination;

CREATE TABLE IF NOT EXISTS execution_coordination.schema_migrations (
    version text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE execution_coordination.capture_runs (
    tenant_id text NOT NULL,
    run_id uuid NOT NULL,
    request_id text NOT NULL,
    idempotency_key text NOT NULL,
    intent_digest text NOT NULL CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    binding jsonb NOT NULL,
    deadline timestamptz NOT NULL,
    max_attempts smallint NOT NULL CHECK (max_attempts BETWEEN 1 AND 5),
    state text NOT NULL CHECK (state IN (
        'accepted', 'running', 'cancel_requested', 'finalization_pending',
        'succeeded', 'failed', 'cancelled', 'timed_out'
    )),
    generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
    attempt_count smallint NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    current_attempt_id uuid,
    cancel_requested_at timestamptz,
    terminal_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    PRIMARY KEY (tenant_id, run_id),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE execution_coordination.capture_attempts (
    tenant_id text NOT NULL,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation >= 1),
    state text NOT NULL CHECK (state IN (
        'leased', 'running', 'retryable_failure', 'failed', 'expired',
        'finalization_pending', 'succeeded', 'cancelled'
    )),
    lease_owner text,
    lease_token text,
    lease_until timestamptz,
    attempt_deadline timestamptz NOT NULL,
    diagnostic_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (tenant_id, attempt_id),
    UNIQUE (tenant_id, run_id, generation),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES execution_coordination.capture_runs(tenant_id, run_id),
    CHECK ((lease_token IS NULL) = (lease_until IS NULL))
);

CREATE TABLE execution_coordination.finalization_reservations (
    tenant_id text NOT NULL,
    reservation_id text NOT NULL,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation >= 1),
    output_set_id text NOT NULL,
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    operation_id text NOT NULL,
    grant_document jsonb NOT NULL,
    publication_receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, reservation_id),
    UNIQUE (tenant_id, run_id),
    UNIQUE (tenant_id, operation_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES execution_coordination.capture_runs(tenant_id, run_id),
    FOREIGN KEY (tenant_id, attempt_id)
        REFERENCES execution_coordination.capture_attempts(tenant_id, attempt_id),
    CHECK ((publication_receipt IS NULL) = (completed_at IS NULL))
);

CREATE TABLE execution_coordination.command_receipts (
    tenant_id text NOT NULL,
    operation_id text NOT NULL,
    intent_digest text NOT NULL CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    result_type text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, operation_id)
);

CREATE TABLE execution_coordination.execution_outbox (
    event_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    run_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'data-mozaic.execution.capture-run-accepted.v1',
        'data-mozaic.execution.capture-attempt-started.v1',
        'data-mozaic.execution.capture-attempt-expired.v1',
        'data-mozaic.execution.finalization-reserved.v1',
        'data-mozaic.execution.capture-run-completed.v1',
        'data-mozaic.execution.capture-run-failed.v1',
        'data-mozaic.execution.capture-run-cancelled.v1',
        'data-mozaic.execution.capture-run-timed-out.v1'
    )),
    payload jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivered')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    delivered_at timestamptz,
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES execution_coordination.capture_runs(tenant_id, run_id)
);

CREATE INDEX capture_runs_recovery_idx
    ON execution_coordination.capture_runs(state, updated_at, tenant_id, run_id)
    WHERE state IN ('accepted', 'running', 'finalization_pending');
CREATE INDEX capture_attempts_expired_lease_idx
    ON execution_coordination.capture_attempts(lease_until, tenant_id, run_id)
    WHERE state IN ('leased', 'running');
CREATE INDEX execution_outbox_pending_idx
    ON execution_coordination.execution_outbox(tenant_id, created_at, event_id)
    WHERE state = 'pending';

GRANT USAGE ON SCHEMA execution_coordination TO bc04_app;
GRANT SELECT, INSERT ON execution_coordination.schema_migrations TO bc04_app;
GRANT SELECT, INSERT, UPDATE ON execution_coordination.capture_runs TO bc04_app;
GRANT SELECT, INSERT, UPDATE ON execution_coordination.capture_attempts TO bc04_app;
GRANT SELECT, INSERT, UPDATE ON execution_coordination.finalization_reservations TO bc04_app;
GRANT SELECT, INSERT ON execution_coordination.command_receipts TO bc04_app;
GRANT SELECT, INSERT, UPDATE ON execution_coordination.execution_outbox TO bc04_app;
