ALTER TABLE scout_custody_intents
    ADD COLUMN expected_size_bytes bigint CHECK (expected_size_bytes IS NULL OR expected_size_bytes >= 0);
