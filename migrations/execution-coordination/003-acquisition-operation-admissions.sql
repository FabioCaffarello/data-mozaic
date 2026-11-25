CREATE TABLE execution_coordination.acquisition_operation_admissions (
    tenant_id text NOT NULL,
    operation_id text NOT NULL,
    run_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    generation integer NOT NULL CHECK (generation >= 1),
    capability_id text NOT NULL,
    target_digest text NOT NULL CHECK (target_digest ~ '^sha256:[0-9a-f]{64}$'),
    admitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, operation_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES execution_coordination.capture_runs(tenant_id, run_id),
    FOREIGN KEY (tenant_id, attempt_id)
        REFERENCES execution_coordination.capture_attempts(tenant_id, attempt_id)
);

CREATE INDEX acquisition_operation_admissions_attempt_idx
    ON execution_coordination.acquisition_operation_admissions
       (tenant_id, run_id, attempt_id, generation, admitted_at);

GRANT SELECT, INSERT ON execution_coordination.acquisition_operation_admissions TO bc04_app;
