CREATE SCHEMA IF NOT EXISTS acquisition_policy;

CREATE TABLE acquisition_policy.policy_revisions (
    policy_id text NOT NULL,
    policy_version text NOT NULL,
    policy_digest text NOT NULL CHECK (policy_digest ~ '^sha256:[0-9a-f]{64}$'),
    document jsonb NOT NULL,
    registered_by text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (policy_id, policy_version)
);

CREATE TABLE acquisition_policy.policy_eligibility (
    policy_id text NOT NULL,
    policy_version text NOT NULL,
    state text NOT NULL CHECK (state IN ('eligible', 'revoked')),
    changed_by text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (policy_id, policy_version),
    FOREIGN KEY (policy_id, policy_version)
        REFERENCES acquisition_policy.policy_revisions(policy_id, policy_version)
);

CREATE TABLE acquisition_policy.policy_revision_events (
    event_id text PRIMARY KEY,
    policy_id text NOT NULL,
    policy_version text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('registered', 'revoked', 'reinstated')),
    actor_id text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (policy_id, policy_version)
        REFERENCES acquisition_policy.policy_revisions(policy_id, policy_version)
);
