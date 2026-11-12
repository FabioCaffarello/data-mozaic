CREATE TABLE scout_custody_intents (
    artifact_id text PRIMARY KEY,
    session_id text NOT NULL REFERENCES scout_sessions(session_id),
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('pending', 'unknown', 'verified')),
    artifact_ref jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status = 'verified') = (artifact_ref IS NOT NULL))
);
CREATE INDEX scout_custody_session_idx ON scout_custody_intents(session_id);
