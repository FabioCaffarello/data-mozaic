CREATE SCHEMA IF NOT EXISTS collector_engineering;

CREATE TABLE collector_engineering.playbook_revisions (
    playbook_id text NOT NULL,
    playbook_digest text NOT NULL CHECK (playbook_digest ~ '^sha256:[0-9a-f]{64}$'),
    source_id text NOT NULL,
    source_revision_id text NOT NULL,
    content bytea NOT NULL CHECK (octet_length(content) <= 1000000),
    registered_by text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (playbook_id, playbook_digest)
);

CREATE TABLE collector_engineering.verified_bot_builds (
    build_id text PRIMARY KEY,
    manifest_digest text NOT NULL CHECK (manifest_digest ~ '^sha256:[0-9a-f]{64}$'),
    runtime_image_digest text NOT NULL CHECK (runtime_image_digest ~ '^sha256:[0-9a-f]{64}$'),
    artifact_path text NOT NULL,
    manifest jsonb NOT NULL,
    registered_by text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE collector_engineering.bot_releases (
    release_id text PRIMARY KEY,
    candidate_digest text NOT NULL CHECK (candidate_digest ~ '^sha256:[0-9a-f]{64}$'),
    candidate jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'approved', 'revoked')),
    approved_release jsonb,
    approved_by text,
    approved_at timestamptz,
    registered_by text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revoked_by text,
    revoked_at timestamptz,
    CHECK ((state = 'pending') = (approved_release IS NULL)),
    CHECK ((state = 'pending') = (approved_at IS NULL)),
    CHECK ((state = 'pending') = (approved_by IS NULL))
);

CREATE TABLE collector_engineering.release_events (
    event_id text PRIMARY KEY,
    release_id text NOT NULL REFERENCES collector_engineering.bot_releases(release_id),
    event_type text NOT NULL CHECK (event_type IN ('candidate-created', 'approved', 'revoked')),
    actor_id text NOT NULL,
    evidence_ref text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE collector_engineering.authority_publications (
    authority_id text NOT NULL,
    generation bigint NOT NULL CHECK (generation >= 1),
    snapshot_digest text NOT NULL CHECK (snapshot_digest ~ '^sha256:[0-9a-f]{64}$'),
    envelope_digest text NOT NULL CHECK (envelope_digest ~ '^sha256:[0-9a-f]{64}$'),
    signed_envelope jsonb NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'installed')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    installed_at timestamptz,
    PRIMARY KEY (authority_id, generation),
    CHECK ((state = 'installed') = (installed_at IS NOT NULL))
);
