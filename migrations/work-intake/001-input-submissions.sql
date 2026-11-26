CREATE SCHEMA IF NOT EXISTS work_intake;

CREATE TABLE work_intake.logical_inputs (
    tenant_id text NOT NULL,
    input_id text NOT NULL,
    source_id text NOT NULL,
    source_revision_id text NOT NULL,
    contract_id text NOT NULL,
    contract_version text NOT NULL,
    contract_digest text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, input_id)
);

CREATE TABLE work_intake.submissions (
    tenant_id text NOT NULL,
    submission_id uuid NOT NULL,
    input_id text NOT NULL,
    source_id text NOT NULL,
    source_revision_id text NOT NULL,
    idempotency_scope text NOT NULL,
    idempotency_key text NOT NULL,
    intent_digest text NOT NULL,
    release_id text NOT NULL,
    contract_id text NOT NULL,
    contract_version text NOT NULL,
    contract_digest text NOT NULL,
    parameters_json bytea NOT NULL,
    parameters_digest text NOT NULL,
    dispatch_operation_id text NOT NULL,
    dispatch_state text NOT NULL CHECK (dispatch_state IN ('pending','claimed','admitted','rejected','cancelled')),
    claim_owner text,
    claim_until timestamptz,
    claim_generation bigint NOT NULL DEFAULT 0 CHECK (claim_generation >= 0),
    run_id uuid,
    diagnostic_code text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, submission_id),
    UNIQUE (tenant_id, idempotency_scope, idempotency_key),
    UNIQUE (tenant_id, dispatch_operation_id),
    FOREIGN KEY (tenant_id, input_id)
      REFERENCES work_intake.logical_inputs (tenant_id, input_id),
    CHECK (octet_length(parameters_json) <= 64000),
    CHECK (parameters_digest ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (intent_digest ~ '^sha256:[0-9a-f]{64}$'),
    CHECK ((dispatch_state = 'claimed') = (claim_owner IS NOT NULL AND claim_until IS NOT NULL)),
    CHECK ((dispatch_state = 'admitted') = (run_id IS NOT NULL)),
    CHECK ((dispatch_state = 'rejected') = (diagnostic_code IS NOT NULL))
);

CREATE INDEX work_intake_pending_dispatch
    ON work_intake.submissions (created_at, tenant_id, submission_id)
    WHERE dispatch_state IN ('pending','claimed');
