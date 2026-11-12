CREATE TABLE scout_sessions (
    session_id text PRIMARY KEY,
    version bigint NOT NULL CHECK (version >= 0),
    document_version integer NOT NULL CHECK (document_version = 1),
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE scout_command_receipts (
    principal text NOT NULL,
    idempotency_key text NOT NULL,
    command_id text NOT NULL,
    command_kind text NOT NULL,
    fingerprint text NOT NULL,
    session_id text NOT NULL REFERENCES scout_sessions(session_id),
    status text NOT NULL CHECK (status IN ('in_progress', 'completed', 'uncertain')),
    response_snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (principal, idempotency_key),
    UNIQUE (command_id)
);
CREATE INDEX scout_receipts_session_idx ON scout_command_receipts(session_id);

CREATE TABLE scout_outbox (
    event_id text PRIMARY KEY,
    aggregate_id text NOT NULL REFERENCES scout_sessions(session_id),
    aggregate_version bigint NOT NULL,
    event_kind text NOT NULL,
    channel text NOT NULL,
    proposal_id text NOT NULL,
    payload jsonb NOT NULL,
    publication_state text NOT NULL CHECK (publication_state IN ('pending', 'sent', 'uncertain')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    transport_receipt jsonb,
    business_ack jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel, proposal_id)
);
CREATE INDEX scout_outbox_pending_idx ON scout_outbox(next_attempt_at)
    WHERE publication_state IN ('pending', 'uncertain');

CREATE TABLE IF NOT EXISTS scout_schema_migrations (
    migration_name text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
