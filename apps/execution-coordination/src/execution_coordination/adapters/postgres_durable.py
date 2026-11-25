"""PostgreSQL adapter for durable BC04 Run, lease, grant, receipt and outbox state."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import psycopg
import rfc8785
from execution_coordination.domain import CoordinationError, ExecutionBinding
from execution_coordination.durable_domain import (
    AttemptLease,
    DurableAttemptState,
    DurableRunIntent,
    DurableRunSnapshot,
    DurableRunState,
    FailureDisposition,
    FinalizationGrant,
    FinalizationReservation,
)
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

MIGRATION = (
    Path(__file__).resolve().parents[5]
    / "migrations"
    / "execution-coordination"
    / "001-durable-runs.sql"
)


def _canonical_digest(value: object) -> str:
    try:
        encoded = rfc8785.dumps(value)
    except Exception as error:
        raise CoordinationError("command intent is not canonical JSON") from error
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _grant_dict(grant: FinalizationGrant) -> dict[str, object]:
    return grant.fields()


def _grant_from_dict(value: dict[str, Any]) -> FinalizationGrant:
    result = dict(value)
    issued = result.get("issued_at")
    if isinstance(issued, str):
        result["issued_at"] = datetime.fromisoformat(issued)
    return FinalizationGrant(**result)


class PostgresDurableRunRepository:
    """Synchronous, short-transaction BC04 adapter; DB time owns lease decisions."""

    def __init__(self, dsn: str, *, connect_timeout: int = 5) -> None:
        if not dsn or not 1 <= connect_timeout <= 30:
            raise CoordinationError("PostgreSQL adapter configuration is invalid")
        self._dsn = dsn
        self._connect_timeout = connect_timeout

    def _connect(self):
        return psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout,
            row_factory=dict_row,
        )

    def migrate(self) -> None:
        """Apply the owner migration once and detect checksum drift."""
        sql = MIGRATION.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        with self._connect() as conn, conn.transaction():
            conn.execute("CREATE SCHEMA IF NOT EXISTS execution_coordination")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS execution_coordination.schema_migrations (
                    version text PRIMARY KEY, checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )"""
            )
            previous = conn.execute(
                "SELECT checksum FROM execution_coordination.schema_migrations WHERE version=%s",
                ("001-durable-runs",),
            ).fetchone()
            if previous is not None:
                if previous["checksum"] != checksum:
                    raise CoordinationError("BC04 migration checksum drift detected")
                return
            # Migration DDL is executed by the owner identity. It is fixed repository SQL.
            conn.execute(sql)
            conn.execute(
                "INSERT INTO execution_coordination.schema_migrations(version,checksum) VALUES (%s,%s)",
                ("001-durable-runs", checksum),
            )

    def create(self, intent: DurableRunIntent) -> DurableRunSnapshot:
        digest = _canonical_digest(
            {
                "command": "CreateCaptureRun/v1",
                "tenant_id": intent.tenant_id,
                "run_id": intent.run_id,
                "request_id": intent.request_id,
                "idempotency_key": intent.idempotency_key,
                "binding": asdict(intent.binding),
                "deadline": intent.deadline.astimezone(UTC).isoformat(),
                "max_attempts": intent.max_attempts,
            }
        )
        with self._connect() as conn, conn.transaction():
            inserted = conn.execute(
                """INSERT INTO execution_coordination.capture_runs
                   (tenant_id,run_id,request_id,idempotency_key,intent_digest,binding,
                    deadline,max_attempts,state)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'accepted')
                   ON CONFLICT (tenant_id,idempotency_key) DO NOTHING
                   RETURNING run_id""",
                (
                    intent.tenant_id,
                    intent.run_id,
                    intent.request_id,
                    intent.idempotency_key,
                    digest,
                    Jsonb(asdict(intent.binding)),
                    intent.deadline.astimezone(UTC),
                    intent.max_attempts,
                ),
            ).fetchone()
            if inserted is None:
                previous = conn.execute(
                    """SELECT run_id,intent_digest FROM execution_coordination.capture_runs
                       WHERE tenant_id=%s AND idempotency_key=%s FOR UPDATE""",
                    (intent.tenant_id, intent.idempotency_key),
                ).fetchone()
                if previous is None or previous["intent_digest"] != digest:
                    raise CoordinationError(
                        "Run idempotency key was reused with different intent"
                    )
                snapshot = self._snapshot(conn, intent.tenant_id, previous["run_id"])
                if snapshot is None:
                    raise CoordinationError("idempotent CaptureRun disappeared")
                return snapshot
            self._event(
                conn,
                intent.tenant_id,
                intent.run_id,
                "data-mozaic.execution.capture-run-accepted.v1",
                {"run_id": intent.run_id, "tenant_id": intent.tenant_id},
            )
            snapshot = self._snapshot(conn, intent.tenant_id, intent.run_id)
            assert snapshot is not None
            return snapshot

    def get(self, tenant_id: str, run_id: str) -> DurableRunSnapshot | None:
        _validate_identity(tenant_id, "tenant_id")
        _validate_identity(run_id, "run_id")
        with self._connect() as conn:
            return self._snapshot(conn, tenant_id, run_id)

    def _snapshot(
        self, conn: psycopg.Connection, tenant_id: str, run_id: Any
    ) -> DurableRunSnapshot | None:
        row = conn.execute(
            """SELECT r.*, a.state AS attempt_state, a.lease_until,
                      fr.reservation_id, fr.attempt_id AS reservation_attempt_id,
                      fr.generation AS reservation_generation, fr.output_set_id,
                      fr.manifest_digest, fr.operation_id AS reservation_operation_id,
                      fr.grant_document, fr.created_at AS reservation_created_at,
                      fr.publication_receipt
               FROM execution_coordination.capture_runs r
               LEFT JOIN execution_coordination.capture_attempts a
                 ON a.tenant_id=r.tenant_id AND a.attempt_id=r.current_attempt_id
               LEFT JOIN execution_coordination.finalization_reservations fr
                 ON fr.tenant_id=r.tenant_id AND fr.run_id=r.run_id
               WHERE r.tenant_id=%s AND r.run_id=%s""",
            (tenant_id, run_id),
        ).fetchone()
        if row is None:
            return None
        intent = DurableRunIntent(
            tenant_id=row["tenant_id"],
            run_id=str(row["run_id"]),
            request_id=row["request_id"],
            idempotency_key=row["idempotency_key"],
            binding=ExecutionBinding(**row["binding"]),
            deadline=row["deadline"],
            max_attempts=row["max_attempts"],
        )
        reservation = None
        if row["reservation_id"] is not None:
            grant = _grant_from_dict(row["grant_document"])
            reservation = FinalizationReservation(
                reservation_id=row["reservation_id"],
                tenant_id=tenant_id,
                run_id=str(row["run_id"]),
                attempt_id=str(row["reservation_attempt_id"]),
                fencing_generation=row["reservation_generation"],
                output_set_id=row["output_set_id"],
                manifest_digest=row["manifest_digest"],
                operation_id=row["reservation_operation_id"],
                grant=grant,
                created_at=row["reservation_created_at"],
            )
        return DurableRunSnapshot(
            intent=intent,
            state=DurableRunState(row["state"]),
            generation=row["generation"],
            attempt_count=row["attempt_count"],
            attempt_state=(
                DurableAttemptState(row["attempt_state"])
                if row["attempt_state"] is not None
                else None
            ),
            lease_until=row["lease_until"],
            reservation=reservation,
            publication_receipt=row["publication_receipt"],
            cancel_requested_at=row["cancel_requested_at"],
            terminal_reason=row["terminal_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            finished_at=row["finished_at"],
        )

    def claim(
        self,
        tenant_id: str,
        run_id: str,
        worker_id: str,
        lease_token: str,
        operation_id: str,
        lease_seconds: int,
    ) -> AttemptLease:
        _validate_lease_args(worker_id, lease_token, operation_id, lease_seconds)
        intent = _canonical_digest(
            {
                "command": "AcquireCaptureAttempt/v1",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "worker_id": worker_id,
                "lease_seconds": lease_seconds,
            }
        )
        with self._connect() as conn, conn.transaction():
            replay = self._replay(conn, tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "attempt_lease":
                    raise CoordinationError("operation id belongs to another command")
                return _lease_from_dict(replay["result"])
            run = self._lock_run(conn, tenant_id, run_id)
            replay = self._replay(conn, tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "attempt_lease":
                    raise CoordinationError("operation id belongs to another command")
                return _lease_from_dict(replay["result"])
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if run["deadline"] <= now:
                raise CoordinationError("CaptureRun deadline has expired")
            if run["state"] == DurableRunState.RUNNING.value:
                current = conn.execute(
                    """SELECT * FROM execution_coordination.capture_attempts
                       WHERE tenant_id=%s AND attempt_id=%s FOR UPDATE""",
                    (tenant_id, run["current_attempt_id"]),
                ).fetchone()
                if current and current["lease_until"] > now:
                    raise CoordinationError("CaptureRun has a current unexpired lease")
                if current:
                    if run["attempt_count"] >= run["max_attempts"]:
                        raise CoordinationError(
                            "expired attempt budget requires reconciliation"
                        )
                    conn.execute(
                        """UPDATE execution_coordination.capture_attempts
                           SET state='expired',lease_owner=NULL,lease_token=NULL,
                               lease_until=NULL,finished_at=clock_timestamp(),
                               diagnostic_code='LEASE_EXPIRED'
                           WHERE tenant_id=%s AND attempt_id=%s""",
                        (tenant_id, current["attempt_id"]),
                    )
                    self._event(
                        conn,
                        tenant_id,
                        run_id,
                        "data-mozaic.execution.capture-attempt-expired.v1",
                        {"run_id": str(run_id), "generation": current["generation"]},
                    )
                    run["state"] = DurableRunState.ACCEPTED.value
            if run["state"] != DurableRunState.ACCEPTED.value:
                raise CoordinationError("CaptureRun cannot acquire an attempt now")
            if run["attempt_count"] >= run["max_attempts"]:
                raise CoordinationError("CaptureRun attempt budget is exhausted")
            generation = run["generation"] + 1
            attempt_id = str(
                uuid5(NAMESPACE_URL, f"data-mozaic:bc04:{run_id}:{generation}")
            )
            lease = conn.execute(
                """INSERT INTO execution_coordination.capture_attempts
                   (tenant_id,run_id,attempt_id,generation,state,lease_owner,lease_token,
                    lease_until,attempt_deadline,started_at)
                   VALUES (%s,%s,%s,%s,'running',%s,%s,
                       LEAST(%s,clock_timestamp()+%s*interval '1 second'),
                       LEAST(%s,clock_timestamp()+%s*interval '1 second'),clock_timestamp())
                   RETURNING lease_until,attempt_deadline""",
                (
                    tenant_id,
                    run_id,
                    attempt_id,
                    generation,
                    worker_id,
                    lease_token,
                    run["deadline"],
                    lease_seconds,
                    run["deadline"],
                    run["binding"]["max_duration_seconds"],
                ),
            ).fetchone()
            conn.execute(
                """UPDATE execution_coordination.capture_runs SET state='running',
                   generation=%s,attempt_count=attempt_count+1,current_attempt_id=%s,
                   updated_at=clock_timestamp() WHERE tenant_id=%s AND run_id=%s""",
                (generation, attempt_id, tenant_id, run_id),
            )
            result = AttemptLease(
                tenant_id,
                str(run_id),
                attempt_id,
                generation,
                worker_id,
                lease_token,
                lease["lease_until"],
                lease["attempt_deadline"],
            )
            self._event(
                conn,
                tenant_id,
                run_id,
                "data-mozaic.execution.capture-attempt-started.v1",
                {
                    "run_id": str(run_id),
                    "attempt_id": attempt_id,
                    "generation": generation,
                },
            )
            self._remember(
                conn,
                tenant_id,
                operation_id,
                intent,
                "attempt_lease",
                _lease_to_dict(result),
            )
            return result

    def renew(
        self, lease: AttemptLease, operation_id: str, lease_seconds: int
    ) -> AttemptLease:
        _validate_identity(operation_id, "operation_id")
        if not 1 <= lease_seconds <= 300:
            raise CoordinationError("lease duration is outside the supported range")
        intent = _canonical_digest(
            {
                "command": "RenewCaptureAttempt/v1",
                "tenant_id": lease.tenant_id,
                "run_id": lease.run_id,
                "attempt_id": lease.attempt_id,
                "generation": lease.generation,
                "lease_token": lease.lease_token,
                "lease_seconds": lease_seconds,
            }
        )
        with self._connect() as conn, conn.transaction():
            replay = self._replay(conn, lease.tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "attempt_lease":
                    raise CoordinationError("operation id belongs to another command")
                return _lease_from_dict(replay["result"])
            run = self._lock_run(conn, lease.tenant_id, lease.run_id)
            replay = self._replay(conn, lease.tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "attempt_lease":
                    raise CoordinationError("operation id belongs to another command")
                return _lease_from_dict(replay["result"])
            if run["state"] != DurableRunState.RUNNING.value:
                raise CoordinationError("CaptureRun is no longer running")
            row = conn.execute(
                """UPDATE execution_coordination.capture_attempts a SET
                   lease_until=LEAST(a.attempt_deadline,
                       clock_timestamp()+%s*interval '1 second')
                   WHERE a.tenant_id=%s AND a.run_id=%s AND a.attempt_id=%s
                     AND a.generation=%s AND a.lease_token=%s
                     AND a.state='running' AND a.lease_until>clock_timestamp()
                     AND a.attempt_deadline>clock_timestamp()
                     AND %s='running'
                   RETURNING a.lease_owner,a.lease_until,a.attempt_deadline""",
                (
                    lease_seconds,
                    lease.tenant_id,
                    lease.run_id,
                    lease.attempt_id,
                    lease.generation,
                    lease.lease_token,
                    run["state"],
                ),
            ).fetchone()
            if row is None:
                raise CoordinationError("attempt lease is stale or no longer renewable")
            result = AttemptLease(
                lease.tenant_id,
                lease.run_id,
                lease.attempt_id,
                lease.generation,
                row["lease_owner"],
                lease.lease_token,
                row["lease_until"],
                row["attempt_deadline"],
            )
            self._remember(
                conn,
                lease.tenant_id,
                operation_id,
                intent,
                "attempt_lease",
                _lease_to_dict(result),
            )
            return result

    def fail_attempt(
        self,
        lease: AttemptLease,
        disposition: FailureDisposition,
        diagnostic_code: str,
        operation_id: str,
    ) -> DurableRunSnapshot:
        _validate_identity(operation_id, "operation_id")
        if not diagnostic_code or len(diagnostic_code) > 128:
            raise CoordinationError("diagnostic_code is invalid")
        intent = _canonical_digest(
            {
                "command": "ReportAttemptFailure/v1",
                "tenant_id": lease.tenant_id,
                "run_id": lease.run_id,
                "attempt_id": lease.attempt_id,
                "generation": lease.generation,
                "lease_token": lease.lease_token,
                "disposition": disposition.value,
                "diagnostic_code": diagnostic_code,
            }
        )
        with self._connect() as conn, conn.transaction():
            replay = self._replay(conn, lease.tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "run_snapshot":
                    raise CoordinationError("operation id belongs to another command")
                return self._snapshot(conn, lease.tenant_id, replay["result"]["run_id"])
            run = self._lock_run(conn, lease.tenant_id, lease.run_id)
            replay = self._replay(conn, lease.tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "run_snapshot":
                    raise CoordinationError("operation id belongs to another command")
                return self._snapshot(conn, lease.tenant_id, lease.run_id)
            attempt = conn.execute(
                """SELECT * FROM execution_coordination.capture_attempts
                   WHERE tenant_id=%s AND run_id=%s AND attempt_id=%s
                     AND generation=%s FOR UPDATE""",
                (lease.tenant_id, lease.run_id, lease.attempt_id, lease.generation),
            ).fetchone()
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if (
                attempt is None
                or run["state"] != DurableRunState.RUNNING.value
                or str(run["current_attempt_id"]) != lease.attempt_id
                or attempt["lease_token"] != lease.lease_token
                or attempt["lease_until"] is None
                or attempt["lease_until"] <= now
            ):
                raise CoordinationError("stale attempt cannot report a failure")
            if run["deadline"] <= now or disposition == FailureDisposition.TIMED_OUT:
                run_state = DurableRunState.TIMED_OUT
                attempt_state = DurableAttemptState.FAILED
            elif disposition == FailureDisposition.CANCELLED:
                run_state = DurableRunState.CANCELLED
                attempt_state = DurableAttemptState.CANCELLED
            elif (
                disposition == FailureDisposition.RETRYABLE
                and run["attempt_count"] < run["max_attempts"]
            ):
                run_state = DurableRunState.ACCEPTED
                attempt_state = DurableAttemptState.RETRYABLE_FAILURE
            else:
                run_state = DurableRunState.FAILED
                attempt_state = DurableAttemptState.FAILED
            conn.execute(
                """UPDATE execution_coordination.capture_attempts SET state=%s,
                   lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                   diagnostic_code=%s,finished_at=clock_timestamp()
                   WHERE tenant_id=%s AND attempt_id=%s""",
                (
                    attempt_state.value,
                    diagnostic_code,
                    lease.tenant_id,
                    lease.attempt_id,
                ),
            )
            conn.execute(
                """UPDATE execution_coordination.capture_runs SET state=%s,
                   terminal_reason=CASE WHEN %s THEN %s ELSE terminal_reason END,
                   finished_at=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
                   updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND run_id=%s""",
                (
                    run_state.value,
                    run_state != DurableRunState.ACCEPTED,
                    diagnostic_code,
                    run_state != DurableRunState.ACCEPTED,
                    lease.tenant_id,
                    lease.run_id,
                ),
            )
            if run_state != DurableRunState.ACCEPTED:
                self._event(
                    conn,
                    lease.tenant_id,
                    lease.run_id,
                    _terminal_event(run_state),
                    {"run_id": lease.run_id, "diagnostic_code": diagnostic_code},
                )
            self._remember(
                conn,
                lease.tenant_id,
                operation_id,
                intent,
                "run_snapshot",
                {"run_id": lease.run_id},
            )
            return self._snapshot(conn, lease.tenant_id, lease.run_id)

    def reserve_finalization(
        self, lease: AttemptLease, grant: FinalizationGrant
    ) -> FinalizationReservation:
        semantic = {
            "command": "ReserveFinalization/v1",
            "tenant_id": grant.tenant_id,
            "run_id": grant.run_id,
            "attempt_id": grant.attempt_id,
            "generation": grant.fencing_generation,
            "output_set_id": grant.output_set_id,
            "manifest_digest": grant.manifest_digest,
            "reservation_id": grant.reservation_id,
            "operation_id": grant.operation_id,
        }
        intent = _canonical_digest(semantic)
        with self._connect() as conn, conn.transaction():
            replay = self._replay(conn, grant.tenant_id, grant.operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "finalization_reservation":
                    raise CoordinationError("operation id belongs to another command")
                snapshot = self._snapshot(conn, grant.tenant_id, grant.run_id)
                if snapshot is None or snapshot.reservation is None:
                    raise CoordinationError("reserved finalization disappeared")
                return snapshot.reservation
            run = self._lock_run(conn, lease.tenant_id, lease.run_id)
            replay = self._replay(conn, grant.tenant_id, grant.operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "finalization_reservation":
                    raise CoordinationError("operation id belongs to another command")
                snapshot = self._snapshot(conn, grant.tenant_id, grant.run_id)
                if snapshot is None or snapshot.reservation is None:
                    raise CoordinationError("reserved finalization disappeared")
                return snapshot.reservation
            if (
                grant.tenant_id,
                grant.run_id,
                grant.attempt_id,
                grant.fencing_generation,
            ) != (
                lease.tenant_id,
                lease.run_id,
                lease.attempt_id,
                lease.generation,
            ) or grant.operation_id == "":
                raise CoordinationError(
                    "grant does not match the current attempt lease"
                )
            existing = conn.execute(
                """SELECT * FROM execution_coordination.finalization_reservations
                   WHERE tenant_id=%s AND run_id=%s FOR UPDATE""",
                (lease.tenant_id, lease.run_id),
            ).fetchone()
            if existing is not None:
                if not _same_reservation(existing, grant):
                    raise CoordinationError(
                        "CaptureRun already has a conflicting reservation"
                    )
                self._remember(
                    conn,
                    lease.tenant_id,
                    grant.operation_id,
                    intent,
                    "finalization_reservation",
                    {"reservation_id": existing["reservation_id"]},
                )
                snap = self._snapshot(conn, lease.tenant_id, lease.run_id)
                assert snap and snap.reservation
                return snap.reservation
            attempt = conn.execute(
                """SELECT * FROM execution_coordination.capture_attempts
                   WHERE tenant_id=%s AND run_id=%s AND attempt_id=%s AND generation=%s
                   FOR UPDATE""",
                (lease.tenant_id, lease.run_id, lease.attempt_id, lease.generation),
            ).fetchone()
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            if (
                run["state"] != DurableRunState.RUNNING.value
                or str(run["current_attempt_id"]) != lease.attempt_id
                or run["deadline"] <= now
                or attempt is None
                or attempt["state"] != DurableAttemptState.RUNNING.value
                or attempt["lease_token"] != lease.lease_token
                or attempt["lease_until"] is None
                or attempt["lease_until"] <= now
            ):
                raise CoordinationError(
                    "stale or expired attempt cannot reserve finalization"
                )
            conn.execute(
                """INSERT INTO execution_coordination.finalization_reservations
                   (tenant_id,reservation_id,run_id,attempt_id,generation,output_set_id,
                    manifest_digest,operation_id,grant_document)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    grant.tenant_id,
                    grant.reservation_id,
                    grant.run_id,
                    grant.attempt_id,
                    grant.fencing_generation,
                    grant.output_set_id,
                    grant.manifest_digest,
                    grant.operation_id,
                    Jsonb(_grant_dict(grant)),
                ),
            )
            conn.execute(
                """UPDATE execution_coordination.capture_attempts SET
                   state='finalization_pending',lease_owner=NULL,lease_token=NULL,
                   lease_until=NULL WHERE tenant_id=%s AND attempt_id=%s""",
                (lease.tenant_id, lease.attempt_id),
            )
            conn.execute(
                """UPDATE execution_coordination.capture_runs SET
                   state='finalization_pending',updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND run_id=%s""",
                (lease.tenant_id, lease.run_id),
            )
            self._event(
                conn,
                grant.tenant_id,
                grant.run_id,
                "data-mozaic.execution.finalization-reserved.v1",
                {
                    "run_id": grant.run_id,
                    "attempt_id": grant.attempt_id,
                    "generation": grant.fencing_generation,
                    "reservation_id": grant.reservation_id,
                    "output_set_id": grant.output_set_id,
                    "manifest_digest": grant.manifest_digest,
                },
            )
            self._remember(
                conn,
                grant.tenant_id,
                grant.operation_id,
                intent,
                "finalization_reservation",
                {"reservation_id": grant.reservation_id},
            )
            snapshot = self._snapshot(conn, grant.tenant_id, grant.run_id)
            assert snapshot and snapshot.reservation
            return snapshot.reservation

    def complete_finalization(
        self,
        tenant_id: str,
        run_id: str,
        reservation_id: str,
        receipt: Any,
        operation_id: str,
    ) -> DurableRunSnapshot:
        for value, label in (
            (tenant_id, "tenant_id"),
            (run_id, "run_id"),
            (reservation_id, "reservation_id"),
            (operation_id, "operation_id"),
        ):
            _validate_identity(value, label)
        receipt_dict = dict(receipt)
        intent = _canonical_digest(
            {
                "command": "CompleteCaptureRun/v1",
                "tenant_id": tenant_id,
                "run_id": run_id,
                "reservation_id": reservation_id,
                "publication_receipt": receipt_dict,
            }
        )
        with self._connect() as conn, conn.transaction():
            replay = self._replay(conn, tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "run_snapshot":
                    raise CoordinationError("operation id belongs to another command")
                return self._snapshot(conn, tenant_id, run_id)
            run = self._lock_run(conn, tenant_id, run_id)
            replay = self._replay(conn, tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "run_snapshot":
                    raise CoordinationError("operation id belongs to another command")
                return self._snapshot(conn, tenant_id, run_id)
            reservation = conn.execute(
                """SELECT * FROM execution_coordination.finalization_reservations
                   WHERE tenant_id=%s AND run_id=%s AND reservation_id=%s FOR UPDATE""",
                (tenant_id, run_id, reservation_id),
            ).fetchone()
            if reservation is None:
                raise CoordinationError("finalization reservation does not exist")
            _validate_receipt_for_reservation(receipt_dict, reservation)
            if reservation["publication_receipt"] is not None:
                if reservation["publication_receipt"] != receipt_dict:
                    raise CoordinationError("conflicting PublicationReceipt replay")
            else:
                if run["state"] != DurableRunState.FINALIZATION_PENDING.value:
                    raise CoordinationError("CaptureRun is not awaiting finalization")
                conn.execute(
                    """UPDATE execution_coordination.finalization_reservations SET
                       publication_receipt=%s,completed_at=clock_timestamp()
                       WHERE tenant_id=%s AND reservation_id=%s""",
                    (Jsonb(receipt_dict), tenant_id, reservation_id),
                )
                conn.execute(
                    """UPDATE execution_coordination.capture_attempts SET
                       state='succeeded',finished_at=clock_timestamp()
                       WHERE tenant_id=%s AND attempt_id=%s AND generation=%s""",
                    (tenant_id, reservation["attempt_id"], reservation["generation"]),
                )
                conn.execute(
                    """UPDATE execution_coordination.capture_runs SET state='succeeded',
                       finished_at=clock_timestamp(),updated_at=clock_timestamp()
                       WHERE tenant_id=%s AND run_id=%s""",
                    (tenant_id, run_id),
                )
                self._event(
                    conn,
                    tenant_id,
                    run_id,
                    "data-mozaic.execution.capture-run-completed.v1",
                    {
                        "run_id": run_id,
                        "reservation_id": reservation_id,
                        "output_set_id": reservation["output_set_id"],
                        "manifest_digest": reservation["manifest_digest"],
                        "publication_id": receipt_dict["publication_id"],
                    },
                )
            self._remember(
                conn,
                tenant_id,
                operation_id,
                intent,
                "run_snapshot",
                {"run_id": run_id},
            )
            return self._snapshot(conn, tenant_id, run_id)

    def cancel(
        self, tenant_id: str, run_id: str, operation_id: str
    ) -> DurableRunSnapshot:
        for value, label in (
            (tenant_id, "tenant_id"),
            (run_id, "run_id"),
            (operation_id, "operation_id"),
        ):
            _validate_identity(value, label)
        intent = _canonical_digest(
            {"command": "CancelCaptureRun/v1", "tenant_id": tenant_id, "run_id": run_id}
        )
        with self._connect() as conn, conn.transaction():
            replay = self._replay(conn, tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "run_snapshot":
                    raise CoordinationError("operation id belongs to another command")
                return self._snapshot(conn, tenant_id, run_id)
            run = self._lock_run(conn, tenant_id, run_id)
            replay = self._replay(conn, tenant_id, operation_id, intent)
            if replay is not None:
                if replay["result_type"] != "run_snapshot":
                    raise CoordinationError("operation id belongs to another command")
                return self._snapshot(conn, tenant_id, run_id)
            if run["state"] in {
                DurableRunState.SUCCEEDED.value,
                DurableRunState.FAILED.value,
                DurableRunState.CANCELLED.value,
                DurableRunState.TIMED_OUT.value,
            }:
                pass
            elif run["state"] == DurableRunState.FINALIZATION_PENDING.value:
                # Reservation is durable authorization: cancellation is recorded,
                # but it cannot revoke a grant that BC05 may already be committing.
                conn.execute(
                    """UPDATE execution_coordination.capture_runs SET
                       cancel_requested_at=COALESCE(cancel_requested_at,clock_timestamp()),
                       updated_at=clock_timestamp() WHERE tenant_id=%s AND run_id=%s""",
                    (tenant_id, run_id),
                )
            else:
                now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                state = (
                    DurableRunState.TIMED_OUT
                    if run["deadline"] <= now
                    else DurableRunState.CANCELLED
                )
                if run["current_attempt_id"] is not None:
                    conn.execute(
                        """UPDATE execution_coordination.capture_attempts SET state='cancelled',
                           lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                           finished_at=clock_timestamp(),diagnostic_code='CANCELLED'
                           WHERE tenant_id=%s AND attempt_id=%s
                             AND state IN ('leased','running')""",
                        (tenant_id, run["current_attempt_id"]),
                    )
                self._terminal_run(conn, run, state, "CANCELLED_BY_REQUEST")
                self._event(
                    conn,
                    tenant_id,
                    run_id,
                    _terminal_event(state),
                    {"run_id": run_id},
                )
            self._remember(
                conn,
                tenant_id,
                operation_id,
                intent,
                "run_snapshot",
                {"run_id": run_id},
            )
            return self._snapshot(conn, tenant_id, run_id)

    def expire_leases(self, *, limit: int = 100) -> tuple[DurableRunSnapshot, ...]:
        if not 1 <= limit <= 500:
            raise CoordinationError("recovery batch size must be from 1 through 500")
        changed: list[tuple[str, str]] = []
        with self._connect() as conn, conn.transaction():
            rows = conn.execute(
                """SELECT r.tenant_id,r.run_id,r.deadline,r.attempt_count,r.max_attempts,
                          r.current_attempt_id,a.generation,a.lease_until
                   FROM execution_coordination.capture_runs r
                   LEFT JOIN execution_coordination.capture_attempts a
                     ON a.tenant_id=r.tenant_id AND a.attempt_id=r.current_attempt_id
                   WHERE (r.state='running' AND a.lease_until<=clock_timestamp())
                      OR (r.state='running' AND r.deadline<=clock_timestamp())
                      OR (r.state='accepted' AND r.deadline<=clock_timestamp())
                   ORDER BY COALESCE(a.lease_until,r.deadline),r.tenant_id,r.run_id
                   LIMIT %s FOR UPDATE OF r SKIP LOCKED""",
                (limit,),
            ).fetchall()
            for row in rows:
                now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                if row["deadline"] <= now:
                    if row["current_attempt_id"] is not None:
                        conn.execute(
                            """UPDATE execution_coordination.capture_attempts SET
                               state='expired',lease_owner=NULL,lease_token=NULL,
                               lease_until=NULL,finished_at=clock_timestamp(),
                               diagnostic_code='RUN_DEADLINE_EXPIRED'
                               WHERE tenant_id=%s AND attempt_id=%s
                                 AND state IN ('leased','running')""",
                            (row["tenant_id"], row["current_attempt_id"]),
                        )
                    self._terminal_run(
                        conn,
                        row,
                        DurableRunState.TIMED_OUT,
                        "RUN_DEADLINE_EXPIRED",
                    )
                    self._event(
                        conn,
                        row["tenant_id"],
                        row["run_id"],
                        _terminal_event(DurableRunState.TIMED_OUT),
                        {"run_id": str(row["run_id"])},
                    )
                    changed.append((row["tenant_id"], str(row["run_id"])))
                    continue
                if row["current_attempt_id"] is None:
                    continue
                conn.execute(
                    """UPDATE execution_coordination.capture_attempts SET state='expired',
                       lease_owner=NULL,lease_token=NULL,lease_until=NULL,
                       finished_at=clock_timestamp(),diagnostic_code='LEASE_EXPIRED'
                       WHERE tenant_id=%s AND attempt_id=%s
                         AND state IN ('leased','running')""",
                    (row["tenant_id"], row["current_attempt_id"]),
                )
                event_type = "data-mozaic.execution.capture-attempt-expired.v1"
                if row["attempt_count"] >= row["max_attempts"]:
                    self._terminal_run(
                        conn,
                        row,
                        DurableRunState.FAILED,
                        "ATTEMPT_BUDGET_EXHAUSTED",
                    )
                    self._event(
                        conn,
                        row["tenant_id"],
                        row["run_id"],
                        _terminal_event(DurableRunState.FAILED),
                        {"run_id": str(row["run_id"]), "generation": row["generation"]},
                    )
                else:
                    conn.execute(
                        """UPDATE execution_coordination.capture_runs SET state='accepted',
                           updated_at=clock_timestamp() WHERE tenant_id=%s AND run_id=%s""",
                        (row["tenant_id"], row["run_id"]),
                    )
                    self._event(
                        conn,
                        row["tenant_id"],
                        row["run_id"],
                        event_type,
                        {"run_id": str(row["run_id"]), "generation": row["generation"]},
                    )
                changed.append((row["tenant_id"], str(row["run_id"])))
        return tuple(
            snapshot
            for tenant_id, run_id in changed
            if (snapshot := self.get(tenant_id, run_id)) is not None
        )

    def list_recoverable(self, *, limit: int = 100) -> tuple[DurableRunSnapshot, ...]:
        if not 1 <= limit <= 500:
            raise CoordinationError("recovery batch size must be from 1 through 500")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT tenant_id,run_id FROM execution_coordination.capture_runs
                   WHERE state IN ('accepted','running','finalization_pending')
                   ORDER BY updated_at,tenant_id,run_id LIMIT %s""",
                (limit,),
            ).fetchall()
            return tuple(
                snapshot
                for row in rows
                if (snapshot := self._snapshot(conn, row["tenant_id"], row["run_id"]))
                is not None
            )

    def list_pending_finalizations(
        self,
        *,
        limit: int = 50,
        tenant_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[DurableRunSnapshot, ...]:
        if not 1 <= limit <= 100:
            raise CoordinationError(
                "finalization batch size must be from 1 through 100"
            )
        if (tenant_id is None) != (run_id is None):
            raise CoordinationError("tenant_id and run_id must be supplied together")
        with self._connect() as conn:
            if tenant_id is None:
                rows = conn.execute(
                    """SELECT tenant_id,run_id FROM execution_coordination.capture_runs
                       WHERE state='finalization_pending'
                       ORDER BY updated_at,tenant_id,run_id LIMIT %s""",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT tenant_id,run_id FROM execution_coordination.capture_runs
                       WHERE state='finalization_pending' AND tenant_id=%s AND run_id=%s
                       LIMIT %s""",
                    (tenant_id, run_id, limit),
                ).fetchall()
            return tuple(
                snapshot
                for row in rows
                if (snapshot := self._snapshot(conn, row["tenant_id"], row["run_id"]))
                is not None
            )

    def list_pending_events(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise CoordinationError("outbox page size must be from 1 through 500")
        with self._connect() as conn:
            return tuple(
                dict(row)
                for row in conn.execute(
                    """SELECT event_id,event_type,payload,created_at FROM
                       execution_coordination.execution_outbox
                       WHERE tenant_id=%s AND state='pending'
                       ORDER BY created_at,event_id LIMIT %s""",
                    (tenant_id, limit),
                ).fetchall()
            )

    @staticmethod
    def _lock_run(conn: psycopg.Connection, tenant_id: str, run_id: str):
        row = conn.execute(
            """SELECT * FROM execution_coordination.capture_runs
               WHERE tenant_id=%s AND run_id=%s FOR UPDATE""",
            (tenant_id, run_id),
        ).fetchone()
        if row is None:
            raise CoordinationError("CaptureRun does not exist")
        return row

    @staticmethod
    def _replay(
        conn: psycopg.Connection, tenant_id: str, operation_id: str, intent_digest: str
    ):
        row = conn.execute(
            """SELECT intent_digest,result_type,result
               FROM execution_coordination.command_receipts
               WHERE tenant_id=%s AND operation_id=%s""",
            (tenant_id, operation_id),
        ).fetchone()
        if row is None:
            return None
        if row["intent_digest"] != intent_digest:
            raise CoordinationError("operation identity was reused with changed intent")
        return row

    @staticmethod
    def _remember(
        conn: psycopg.Connection,
        tenant_id: str,
        operation_id: str,
        intent_digest: str,
        result_type: str,
        result: dict[str, object],
    ) -> None:
        conn.execute(
            """INSERT INTO execution_coordination.command_receipts
               (tenant_id,operation_id,intent_digest,result_type,result)
               VALUES (%s,%s,%s,%s,%s)""",
            (tenant_id, operation_id, intent_digest, result_type, Jsonb(result)),
        )

    @staticmethod
    def _event(
        conn: psycopg.Connection,
        tenant_id: str,
        run_id: Any,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        event_id = (
            "evt-"
            + hashlib.sha256(
                _canonical_digest(
                    [event_type, tenant_id, str(run_id), payload]
                ).encode()
            ).hexdigest()
        )
        conn.execute(
            """INSERT INTO execution_coordination.execution_outbox
               (event_id,tenant_id,run_id,event_type,payload)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT(event_id) DO NOTHING""",
            (event_id, tenant_id, run_id, event_type, Jsonb(payload)),
        )

    @staticmethod
    def _terminal_run(
        conn: psycopg.Connection,
        run: dict[str, Any],
        state: DurableRunState,
        reason: str,
    ) -> None:
        conn.execute(
            """UPDATE execution_coordination.capture_runs SET state=%s,
               terminal_reason=%s,finished_at=clock_timestamp(),updated_at=clock_timestamp()
               WHERE tenant_id=%s AND run_id=%s""",
            (state.value, reason, run["tenant_id"], run["run_id"]),
        )


def _validate_lease_args(
    worker_id: str, lease_token: str, operation_id: str, lease_seconds: int
) -> None:
    for value, label in (
        (worker_id, "worker_id"),
        (lease_token, "lease_token"),
        (operation_id, "operation_id"),
    ):
        _validate_identity(value, label)
    if not 1 <= lease_seconds <= 300:
        raise CoordinationError("lease duration is outside the supported range")


def _validate_identity(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise CoordinationError(f"{label} is invalid")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CoordinationError(f"{label} contains invalid Unicode") from error


def _lease_to_dict(lease: AttemptLease) -> dict[str, object]:
    return {
        "tenant_id": lease.tenant_id,
        "run_id": lease.run_id,
        "attempt_id": lease.attempt_id,
        "generation": lease.generation,
        "lease_owner": lease.lease_owner,
        "lease_token": lease.lease_token,
        "lease_until": lease.lease_until.astimezone(UTC).isoformat(),
        "attempt_deadline": lease.attempt_deadline.astimezone(UTC).isoformat(),
    }


def _lease_from_dict(value: dict[str, Any]) -> AttemptLease:
    result = dict(value)
    for key in ("lease_until", "attempt_deadline"):
        if isinstance(result[key], str):
            result[key] = datetime.fromisoformat(result[key])
    return AttemptLease(**result)


def _same_reservation(row: dict[str, Any], grant: FinalizationGrant) -> bool:
    return (
        row["reservation_id"],
        str(row["run_id"]),
        str(row["attempt_id"]),
        row["generation"],
        row["output_set_id"],
        row["manifest_digest"],
        row["operation_id"],
    ) == (
        grant.reservation_id,
        grant.run_id,
        grant.attempt_id,
        grant.fencing_generation,
        grant.output_set_id,
        grant.manifest_digest,
        grant.operation_id,
    )


def _validate_receipt_for_reservation(
    receipt: dict[str, object], reservation: dict[str, Any]
) -> None:
    if (
        receipt.get("contract_version") != "publication-receipt/v1"
        or receipt.get("tenant_id") != reservation["tenant_id"]
        or receipt.get("output_set_id") != reservation["output_set_id"]
        or receipt.get("manifest_digest") != reservation["manifest_digest"]
        or receipt.get("finalization_reservation_id") != reservation["reservation_id"]
        or receipt.get("operation_id") != reservation["operation_id"]
        or not isinstance(receipt.get("publication_id"), str)
        or not receipt.get("publication_id")
        or not isinstance(receipt.get("published_at"), str)
    ):
        raise CoordinationError("PublicationReceipt does not match its reservation")


def _terminal_event(state: DurableRunState) -> str:
    return {
        DurableRunState.SUCCEEDED: "data-mozaic.execution.capture-run-completed.v1",
        DurableRunState.FAILED: "data-mozaic.execution.capture-run-failed.v1",
        DurableRunState.CANCELLED: "data-mozaic.execution.capture-run-cancelled.v1",
        DurableRunState.TIMED_OUT: "data-mozaic.execution.capture-run-timed-out.v1",
    }[state]
