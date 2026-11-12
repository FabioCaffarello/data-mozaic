CREATE TABLE scout_reconciliation_events (
    event_id text PRIMARY KEY,
    artifact_id text NOT NULL,
    session_id text REFERENCES scout_sessions(session_id),
    event_kind text NOT NULL CHECK (event_kind IN ('digest_or_identity_discrepancy', 'orphan_candidate')),
    owner text NOT NULL CHECK (owner IN ('BC14', 'BC06')),
    details jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX scout_reconciliation_artifact_idx
    ON scout_reconciliation_events(artifact_id, occurred_at);
