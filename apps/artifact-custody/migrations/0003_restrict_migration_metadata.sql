-- Migration history is written only by the BC06 provisioning identity.
REVOKE ALL ON artifact_custody.schema_migrations FROM bc06_app;
