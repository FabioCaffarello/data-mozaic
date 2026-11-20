"""Durable private index for Docker-backed BC15 runs."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

MAX_RETAINED_RUNS = 100
MAX_REGISTRY_ROWS = 200
RETENTION = timedelta(hours=24)


class ProtectedRunRegistry:
    """Persist run ownership and allowlisted reports for broker restart recovery."""

    def __init__(self, path: Path):
        self.path = path
        if path.is_symlink():
            raise ValueError("protected BC15 registry path is invalid")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        if path.exists() and (not path.is_file() or path.stat().st_mode & 0o077):
            raise ValueError("protected BC15 registry permissions are invalid")
        self._lock = threading.RLock()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS protected_runs ("
                "run_id TEXT PRIMARY KEY, volume TEXT NOT NULL UNIQUE, "
                "session_id TEXT NOT NULL, principal TEXT NOT NULL, state TEXT NOT NULL, "
                "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, report_json TEXT)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS protected_run_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS approval_claims ("
                "principal TEXT NOT NULL, source_id TEXT NOT NULL, revision TEXT NOT NULL, "
                "expires_at TEXT NOT NULL, claimed_at TEXT NOT NULL, "
                "PRIMARY KEY(principal,source_id,revision,expires_at))"
            )
            row = db.execute(
                "SELECT value FROM protected_run_meta WHERE key='schema'"
            ).fetchone()
            if row and row[0] != "1":
                raise ValueError("protected BC15 registry schema is unsupported")
            db.execute(
                "INSERT OR IGNORE INTO protected_run_meta(key,value) VALUES('schema','1')"
            )
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def create(self, run_id: str, volume: str, session_id: str, principal: str) -> None:
        now = datetime.now(UTC)
        with self._lock, self._connect() as db:
            total = db.execute("SELECT count(*) FROM protected_runs").fetchone()[0]
            while total >= MAX_REGISTRY_ROWS:
                stale = db.execute(
                    "SELECT run_id FROM protected_runs WHERE state='expired' "
                    "ORDER BY expires_at LIMIT 1"
                ).fetchone()
                if stale is None:
                    raise ValueError("protected BC15 registry capacity is full")
                db.execute("DELETE FROM protected_runs WHERE run_id=?", (stale[0],))
                total -= 1
            active = db.execute(
                "SELECT count(*) FROM protected_runs WHERE state IN ('creating','running','passed','incomplete') AND expires_at>?",
                (now.isoformat(),),
            ).fetchone()[0]
            if active >= MAX_RETAINED_RUNS:
                raise ValueError("protected BC15 retention capacity is full")
            db.execute(
                "INSERT INTO protected_runs VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    run_id,
                    volume,
                    session_id,
                    principal,
                    "creating",
                    now.isoformat(),
                    (now + RETENTION).isoformat(),
                ),
            )

    def claim_approval_once(
        self, principal: str, source_id: str, revision: str, expires_at: str
    ) -> None:
        try:
            with self._lock, self._connect() as db:
                db.execute(
                    "INSERT INTO approval_claims VALUES(?,?,?,?,?)",
                    (
                        principal,
                        source_id,
                        revision,
                        expires_at,
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "single-use source approval was already consumed"
            ) from error

    def update(self, run_id: str, state: str, report: dict | None = None) -> None:
        if state not in {"running", "passed", "incomplete", "expired"}:
            raise ValueError("protected BC15 run state is invalid")
        report_json = (
            json.dumps(report, ensure_ascii=False, separators=(",", ":"))
            if report is not None
            else None
        )
        if report_json and len(report_json.encode("utf-8")) > 256_000:
            raise ValueError("protected BC15 report exceeds the output limit")
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE protected_runs SET state=?,report_json=COALESCE(?,report_json) WHERE run_id=?",
                (state, report_json, run_id),
            )

    def get(self, run_id: str) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM protected_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "volume": row["volume"],
            "session_id": row["session_id"],
            "principal": row["principal"],
            "state": row["state"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "report": json.loads(row["report_json"]) if row["report_json"] else None,
        }

    def entries(self) -> list[dict]:
        with self._lock, self._connect() as db:
            ids = [row[0] for row in db.execute("SELECT run_id FROM protected_runs")]
        return [self.get(run_id) for run_id in ids]
