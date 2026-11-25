#!/usr/bin/env python3
"""Create ignored local-only BC06 credentials without printing their values."""

from __future__ import annotations

import secrets
from pathlib import Path

target = Path(".env.artifact-custody")
if target.exists():
    print("Local BC06 environment already exists; preserving it.")
    raise SystemExit(0)


def token(prefix: str = "") -> str:
    return prefix + secrets.token_hex(32)


app_db_password = token()
owner_db_password = token()

lines = {
    "ARTIFACT_CUSTODY_MODE": "local",
    "ARTIFACT_CUSTODY_LOCAL_TOKEN": token(),
    "ARTIFACT_CUSTODY_LISTEN": ":8086",
    "ARTIFACT_CUSTODY_MAX_BYTES": str(100 * 1024 * 1024),
    "ARTIFACT_CUSTODY_MAX_CONCURRENT": "4",
    "ARTIFACT_CUSTODY_TEMP_BUDGET_BYTES": str(420 * 1024 * 1024),
    "ARTIFACT_CUSTODY_TEMP_DIR": "/tmp/bc06-upload",
    "ARTIFACT_CUSTODY_DATABASE_URL": "postgresql://bc06_app:"
    + app_db_password
    + "@postgres:5432/scout_dev?sslmode=disable",
    "ARTIFACT_CUSTODY_MIGRATOR_DATABASE_URL": "postgresql://bc06_owner:"
    + owner_db_password
    + "@postgres:5432/scout_dev?sslmode=disable",
    "ARTIFACT_CUSTODY_MIGRATIONS_DIR": "/migrations",
    "ARTIFACT_CUSTODY_S3_ENDPOINT": "http://rustfs:9000",
    "ARTIFACT_CUSTODY_S3_BUCKET": "bc06-artifacts",
    "ARTIFACT_CUSTODY_S3_ACCESS_KEY": token("bc06app"),
    "ARTIFACT_CUSTODY_S3_SECRET_KEY": token(),
    "RUSTFS_ROOT_ACCESS_KEY": token("bc06root"),
    "RUSTFS_ROOT_SECRET_KEY": token(),
    "AWS_REGION": "us-east-1",
    "BC06_APP_DB_PASSWORD": app_db_password,
    "BC06_OWNER_DB_PASSWORD": owner_db_password,
}
lines["AWS_ACCESS_KEY_ID"] = lines["ARTIFACT_CUSTODY_S3_ACCESS_KEY"]
lines["AWS_SECRET_ACCESS_KEY"] = lines["ARTIFACT_CUSTODY_S3_SECRET_KEY"]
target.write_text(
    "".join(f"{key}={value}\n" for key, value in lines.items()), encoding="utf-8"
)
target.chmod(0o600)
print("Created ignored local BC06 environment with unique random credentials.")
