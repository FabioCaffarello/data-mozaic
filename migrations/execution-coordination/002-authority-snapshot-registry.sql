CREATE TABLE execution_coordination.authority_snapshot_versions (
    authority_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 1),
    snapshot_digest text NOT NULL CHECK (snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    envelope_digest text NOT NULL CHECK (envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
    signing_key_id text NOT NULL,
    expires_at timestamptz NOT NULL,
    signed_envelope jsonb NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (authority_id, generation)
);

CREATE TABLE execution_coordination.authority_snapshot_heads (
    authority_id text PRIMARY KEY,
    generation bigint NOT NULL,
    snapshot_digest text NOT NULL CHECK (snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    envelope_digest text NOT NULL CHECK (envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
    signing_key_id text NOT NULL,
    expires_at timestamptz NOT NULL,
    accepted_at timestamptz NOT NULL,
    FOREIGN KEY (authority_id, generation)
        REFERENCES execution_coordination.authority_snapshot_versions(authority_id, generation)
);

GRANT SELECT, INSERT ON execution_coordination.authority_snapshot_versions TO bc04_app;
GRANT SELECT, INSERT, UPDATE ON execution_coordination.authority_snapshot_heads TO bc04_app;
