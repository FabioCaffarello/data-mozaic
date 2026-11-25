"""Apply the BC04 owner migration from local configuration."""

from __future__ import annotations

import os

from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)

dsn = os.environ.get("BC04_OWNER_DATABASE_URL")
if not dsn:
    raise SystemExit("BC04_OWNER_DATABASE_URL is required")
PostgresDurableRunRepository(dsn).migrate()
print("BC04 durable-run migration is current.")
