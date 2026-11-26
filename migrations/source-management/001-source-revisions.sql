CREATE SCHEMA IF NOT EXISTS source_management;

CREATE TABLE source_management.sources (
    source_id text PRIMARY KEY,
    owner text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE source_management.source_revisions (
    source_id text NOT NULL REFERENCES source_management.sources(source_id),
    revision_id text NOT NULL,
    revision_digest text NOT NULL CHECK (revision_digest ~ '^sha256:[0-9a-f]{64}$'),
    document jsonb NOT NULL,
    registered_by text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source_id, revision_id)
);

CREATE TABLE source_management.source_revision_eligibility (
    source_id text NOT NULL,
    revision_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('eligible', 'revoked')),
    changed_by text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source_id, revision_id),
    FOREIGN KEY (source_id, revision_id)
        REFERENCES source_management.source_revisions(source_id, revision_id)
);

CREATE TABLE source_management.source_revision_events (
    event_id text PRIMARY KEY,
    source_id text NOT NULL,
    revision_id text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN ('registered', 'revoked', 'reinstated')),
    actor_id text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (source_id, revision_id)
        REFERENCES source_management.source_revisions(source_id, revision_id)
);
