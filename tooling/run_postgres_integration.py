"""Run mandatory PostgreSQL tests and fail on an empty or skipped suite."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if not os.environ.get("SCOUT_TEST_DATABASE_URL"):
        raise RuntimeError("SCOUT_TEST_DATABASE_URL is required for PostgreSQL tests")
    suite = unittest.TestLoader().discover(
        str(ROOT / "tests" / "integration"), pattern="test_*.py"
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun == 0:
        print("PostgreSQL integration suite discovered zero tests", file=sys.stderr)
        return 1
    if result.skipped:
        print(
            f"PostgreSQL integration suite skipped {len(result.skipped)} required tests",
            file=sys.stderr,
        )
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
