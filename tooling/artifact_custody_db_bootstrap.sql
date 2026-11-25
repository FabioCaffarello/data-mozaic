\set ON_ERROR_STOP on
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')
\gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
\gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'owner_user', :'owner_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner_user')
\gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'owner_user', :'owner_password')
\gexec
SELECT format('GRANT CREATE ON DATABASE %I TO %I', current_database(), :'owner_user')
\gexec
