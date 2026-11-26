ALTER TABLE collector_engineering.authority_publications
    DROP CONSTRAINT authority_publications_state_check;

ALTER TABLE collector_engineering.authority_publications
    ADD CONSTRAINT authority_publications_state_check
    CHECK (state IN ('pending', 'installed', 'superseded'));

ALTER TABLE collector_engineering.authority_publications
    ADD COLUMN superseded_at timestamptz;

ALTER TABLE collector_engineering.authority_publications
    ADD CONSTRAINT authority_publications_superseded_at_check
    CHECK ((state = 'superseded') = (superseded_at IS NOT NULL));
