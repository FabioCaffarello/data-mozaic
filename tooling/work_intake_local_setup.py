"""Provision isolated ignored local PostgreSQL credentials for BC13."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

target = Path(".env.work-intake")
if target.exists():
    print("Local Work Intake environment exists; preserving it.")
    raise SystemExit(0)

admin_dsn = os.environ.get("SCOUT_DATABASE_URL")
if not admin_dsn:
    raise SystemExit("SCOUT_DATABASE_URL is required for local role provisioning")
app_password = secrets.token_urlsafe(36)
owner_password = secrets.token_urlsafe(36)
with psycopg.connect(admin_dsn, autocommit=True) as conn:
    database = conn.info.dbname
    for role in ("work_intake_app", "work_intake_owner"):
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            raise SystemExit(
                f"PostgreSQL role {role} already exists; refusing to alter its credentials"
            )
    conn.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier("work_intake_app"), sql.Literal(app_password)
        )
    )
    conn.execute(
        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier("work_intake_owner"), sql.Literal(owner_password)
        )
    )
    conn.execute(
        sql.SQL(
            "GRANT CONNECT ON DATABASE {} TO work_intake_app, work_intake_owner"
        ).format(sql.Identifier(database))
    )
    conn.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO work_intake_owner").format(
            sql.Identifier(database)
        )
    )

host_port = os.environ.get("SCOUT_POSTGRES_PORT", "55432")
if not host_port.isdigit() or not 1 <= int(host_port) <= 65535:
    raise SystemExit("SCOUT_POSTGRES_PORT must be a valid local TCP port")
encoded_db = quote(database, safe="")
target.write_text(
    "\n".join(
        (
            f"WORK_INTAKE_DATABASE_URL=postgresql://work_intake_app:{quote(app_password, safe='')}@127.0.0.1:{host_port}/{encoded_db}?sslmode=disable",
            f"WORK_INTAKE_OWNER_DATABASE_URL=postgresql://work_intake_owner:{quote(owner_password, safe='')}@127.0.0.1:{host_port}/{encoded_db}?sslmode=disable",
            "WORK_INTAKE_APP_ROLE=work_intake_app",
            "",
        )
    ),
    encoding="utf-8",
)
target.chmod(0o600)
print("Created ignored BC13 application/owner PostgreSQL credentials.")
