"""Apply pending Scout SQL migrations to a loopback development database."""

import os
import sys
from urllib.parse import urlparse

import psycopg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "apps", "scout"))

from scout.adapters.postgres.repository import PostgresScoutSessionRepository
from scout.domain import IntegrityError


def main() -> int:
    dsn = os.environ.get("SCOUT_DATABASE_URL")
    if not dsn:
        raise SystemExit("SCOUT_DATABASE_URL is required; load the local .env file")
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise SystemExit("db-migrate only accepts a loopback PostgreSQL DSN")
    try:
        PostgresScoutSessionRepository(dsn).migrate()
    except (psycopg.Error, IntegrityError, ValueError) as exc:
        # Driver diagnostics can include connection details; keep output credential-free.
        raise SystemExit(f"Scout migration failed ({type(exc).__name__})") from None
    print("Scout migrations are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
