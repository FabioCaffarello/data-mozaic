ALTER TABLE scout_outbox
    ADD COLUMN proposal_revision integer NOT NULL DEFAULT 1 CHECK (proposal_revision >= 1);

ALTER TABLE scout_outbox
    DROP CONSTRAINT scout_outbox_channel_proposal_id_key;

ALTER TABLE scout_outbox
    ADD CONSTRAINT scout_outbox_channel_proposal_revision_key
    UNIQUE (channel, proposal_id, proposal_revision);
