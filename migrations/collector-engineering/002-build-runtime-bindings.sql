ALTER TABLE collector_engineering.verified_bot_builds
    RENAME TO bot_builds;

CREATE TABLE collector_engineering.verified_bot_builds (
    build_id text NOT NULL REFERENCES collector_engineering.bot_builds(build_id),
    runtime_image_digest text NOT NULL CHECK (runtime_image_digest ~ '^sha256:[0-9a-f]{64}$'),
    registered_by text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (build_id, runtime_image_digest)
);

INSERT INTO collector_engineering.verified_bot_builds
    (build_id, runtime_image_digest, registered_by)
SELECT build_id, runtime_image_digest, registered_by
FROM collector_engineering.bot_builds;
