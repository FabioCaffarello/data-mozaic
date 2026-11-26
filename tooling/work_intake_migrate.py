"""Apply checksummed BC13 migrations and grant only owner-owned tables."""

from __future__ import annotations

import os
from pathlib import Path

from work_intake.adapters.postgres import PostgresWorkIntakeStore

ROOT = Path(__file__).resolve().parents[1]
dsn = os.environ.get("WORK_INTAKE_OWNER_DATABASE_URL")
if not dsn:
    raise SystemExit("WORK_INTAKE_OWNER_DATABASE_URL is required")
role = os.environ.get("WORK_INTAKE_APP_ROLE", "work_intake_app")
store = PostgresWorkIntakeStore(dsn, ROOT / "migrations")
store.migrate()
store.grant_runtime_role(role)
print("Work Intake migrations current; owner-scoped runtime grants applied.")
