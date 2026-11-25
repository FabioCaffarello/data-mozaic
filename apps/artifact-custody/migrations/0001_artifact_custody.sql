CREATE SCHEMA IF NOT EXISTS artifact_custody;

CREATE TABLE IF NOT EXISTS artifact_custody.schema_migrations (
    version text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS artifact_custody.artifact_inventory (
    tenant_id text NOT NULL,
    artifact_id text NOT NULL,
    operation_id text NOT NULL,
    intent_digest text NOT NULL CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    intent jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('pending_upload', 'uploading', 'available', 'unavailable', 'inconsistent')),
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0),
    attempt_id text,
    lease_token text,
    lease_until timestamptz,
    object_provider text,
    object_bucket text,
    object_key text,
    object_version_id text,
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    confirmed_at timestamptz,
    last_reconciled_at timestamptz,
    reconciliation_state text NOT NULL DEFAULT 'not_checked',
    PRIMARY KEY (tenant_id, artifact_id),
    UNIQUE (tenant_id, operation_id),
    CHECK ((state = 'available') = (receipt IS NOT NULL AND confirmed_at IS NOT NULL)),
    CHECK ((state = 'uploading') = (lease_token IS NOT NULL AND lease_until IS NOT NULL AND object_key IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS artifact_custody.custody_operations (
    tenant_id text NOT NULL,
    operation_id text NOT NULL,
    artifact_id text NOT NULL,
    intent_digest text NOT NULL CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, operation_id),
    FOREIGN KEY (tenant_id, artifact_id) REFERENCES artifact_custody.artifact_inventory (tenant_id, artifact_id)
);

CREATE TABLE IF NOT EXISTS artifact_custody.custody_outbox (
    event_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    artifact_id text NOT NULL,
    event_type text NOT NULL CHECK (event_type = 'data-mozaic.artifact.custody-confirmed.v1'),
    event jsonb NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'delivered')),
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id, artifact_id) REFERENCES artifact_custody.artifact_inventory (tenant_id, artifact_id)
);

CREATE INDEX IF NOT EXISTS artifact_inventory_reconciliation_idx
    ON artifact_custody.artifact_inventory (lease_until, updated_at)
    WHERE state = 'uploading';
CREATE INDEX IF NOT EXISTS custody_outbox_pending_idx
    ON artifact_custody.custody_outbox (tenant_id, state, occurred_at, event_id);

GRANT USAGE ON SCHEMA artifact_custody TO bc06_app;
GRANT SELECT, INSERT ON artifact_custody.schema_migrations TO bc06_app;
GRANT SELECT, INSERT, UPDATE ON artifact_custody.artifact_inventory TO bc06_app;
GRANT SELECT, INSERT, UPDATE ON artifact_custody.custody_operations TO bc06_app;
GRANT SELECT, INSERT, UPDATE ON artifact_custody.custody_outbox TO bc06_app;
