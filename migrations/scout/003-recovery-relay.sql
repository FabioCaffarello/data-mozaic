ALTER TABLE scout_outbox
    DROP CONSTRAINT scout_outbox_publication_state_check;
ALTER TABLE scout_outbox
    ADD CONSTRAINT scout_outbox_publication_state_check
    CHECK (publication_state IN ('pending', 'leased', 'sent', 'uncertain', 'retry_scheduled', 'dead_letter'));
ALTER TABLE scout_outbox
    ADD COLUMN lease_owner text,
    ADD COLUMN lease_deadline timestamptz,
    ADD COLUMN fencing_token bigint NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    ADD COLUMN error_class text,
    ADD COLUMN last_result text;
CREATE INDEX scout_outbox_claim_idx
    ON scout_outbox(publication_state, next_attempt_at, created_at);

ALTER TABLE scout_custody_intents
    DROP CONSTRAINT scout_custody_intents_status_check;
ALTER TABLE scout_custody_intents
    ADD CONSTRAINT scout_custody_intents_status_check
    CHECK (status IN ('pending', 'unknown', 'verified', 'discrepancy', 'manual_review'));
ALTER TABLE scout_custody_intents
    ADD COLUMN reconciliation_note text;

CREATE TABLE scout_orphan_reviews (
    review_id text PRIMARY KEY,
    artifact_id text NOT NULL,
    reported_by text NOT NULL CHECK (reported_by = 'BC06'),
    status text NOT NULL CHECK (status IN ('candidate', 'manual_review', 'resolved')),
    diagnostic jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
