"""Create isolated, ignored local BC04 PostgreSQL credentials."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

target = Path(".env.execution-coordination")
if target.exists():
    print("Local BC04 environment exists; preserving it.")
    raise SystemExit(0)

admin_dsn = os.environ.get("SCOUT_DATABASE_URL")
if not admin_dsn:
    raise SystemExit("SCOUT_DATABASE_URL is required for local role provisioning")
app_password = secrets.token_urlsafe(36)
owner_password = secrets.token_urlsafe(36)
with psycopg.connect(admin_dsn, autocommit=True) as conn:
    database = conn.info.dbname
    for role, password in (
        ("bc04_app", app_password),
        ("bc04_owner", owner_password),
    ):
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
        ).fetchone()
        action = "ALTER" if exists else "CREATE"
        conn.execute(
            sql.SQL(f"{action} ROLE {{}} LOGIN PASSWORD {{}}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
    conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO bc04_app, bc04_owner").format(
            sql.Identifier(database)
        )
    )
    conn.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO bc04_owner").format(
            sql.Identifier(database)
        )
    )

host_port = os.environ.get("SCOUT_POSTGRES_PORT", "55432")
if not host_port.isdigit() or not 1 <= int(host_port) <= 65535:
    raise SystemExit("SCOUT_POSTGRES_PORT must be a valid local TCP port")
encoded_db = quote(database, safe="")
lines = {
    "BC04_DATABASE_URL": f"postgresql://bc04_app:{quote(app_password, safe='')}@127.0.0.1:{host_port}/{encoded_db}?sslmode=disable",
    "BC04_OWNER_DATABASE_URL": f"postgresql://bc04_owner:{quote(owner_password, safe='')}@127.0.0.1:{host_port}/{encoded_db}?sslmode=disable",
    "BC04_GRANT_KEY_ID": "local-fixture-v1",
    "BC04_GRANT_SIGNING_KEY": secrets.token_hex(32),
}
target.write_text(
    "".join(f"{key}={value}\n" for key, value in lines.items()), encoding="utf-8"
)
target.chmod(0o600)
print("Created ignored BC04 application/owner credentials and local grant key.")
