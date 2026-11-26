"""Owner-local PostgreSQL persistence for BC13 inputs and dispatch intent."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from mozaic_contract_resolution import ContractReference
from psycopg.rows import dict_row

from work_intake.domain import (
    DispatchState,
    LogicalInput,
    Submission,
    SubmissionIntent,
    WorkIntakeError,
)


class PostgresWorkIntakeStore:
    """Persist BC13-owned state without reading provider-private schemas."""

    def __init__(self, dsn: str, migrations_root: Path, *, connect_timeout: int = 5):
        if not dsn or not 1 <= connect_timeout <= 30:
            raise WorkIntakeError("Work Intake PostgreSQL configuration is invalid")
        self._dsn = dsn
        self._root = migrations_root.resolve(strict=True)
        self._timeout = connect_timeout

    def _connect(self):
        return psycopg.connect(
            self._dsn, connect_timeout=self._timeout, row_factory=dict_row
        )

    def migrate(self) -> None:
        migration = self._root / "work-intake" / "001-input-submissions.sql"
        if migration.is_symlink() or not migration.is_file():
            raise WorkIntakeError("Work Intake migration is unavailable")
        content = migration.read_text(encoding="utf-8")
        checksum = hashlib.sha256(content.encode()).hexdigest()
        with self._connect() as connection, connection.transaction():
            connection.execute("CREATE SCHEMA IF NOT EXISTS work_intake")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS work_intake.schema_migrations (
                    version text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )"""
            )
            previous = connection.execute(
                "SELECT checksum FROM work_intake.schema_migrations WHERE version=%s FOR UPDATE",
                ("001-input-submissions",),
            ).fetchone()
            if previous is not None:
                if previous["checksum"] != checksum:
                    raise WorkIntakeError("Work Intake migration checksum changed")
                return
            connection.execute(content)
            connection.execute(
                "INSERT INTO work_intake.schema_migrations(version,checksum) VALUES(%s,%s)",
                ("001-input-submissions", checksum),
            )

    def grant_runtime_role(self, role_name: str) -> None:
        if (
            not role_name
            or len(role_name) > 63
            or not role_name.replace("_", "").isalnum()
        ):
            raise WorkIntakeError("Work Intake runtime role is invalid")
        from psycopg import sql

        grantee = sql.Identifier(role_name)
        with self._connect() as connection, connection.transaction():
            connection.execute(
                sql.SQL("GRANT USAGE ON SCHEMA work_intake TO {}").format(grantee)
            )
            connection.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT ON work_intake.logical_inputs TO {}"
                ).format(grantee)
            )
            connection.execute(
                sql.SQL("GRANT SELECT, INSERT ON work_intake.submissions TO {}").format(
                    grantee
                )
            )
            connection.execute(
                sql.SQL("REVOKE UPDATE ON work_intake.submissions FROM {}").format(
                    grantee
                )
            )
            connection.execute(
                sql.SQL(
                    "GRANT UPDATE (dispatch_state,claim_owner,claim_until,"
                    "claim_generation,run_id,diagnostic_code,updated_at) "
                    "ON work_intake.submissions TO {}"
                ).format(grantee)
            )
            connection.execute(
                sql.SQL("GRANT SELECT ON work_intake.schema_migrations TO {}").format(
                    grantee
                )
            )

    def accept(
        self, logical_input: LogicalInput, intent: SubmissionIntent
    ) -> Submission:
        if (
            logical_input.tenant_id != intent.tenant_id
            or logical_input.input_id != intent.input_id
            or logical_input.contract != intent.input_contract
        ):
            raise WorkIntakeError("input and submission bindings differ")
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """INSERT INTO work_intake.logical_inputs
                   (tenant_id,input_id,source_id,source_revision_id,contract_id,
                    contract_version,contract_digest)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (tenant_id,input_id) DO NOTHING""",
                (
                    logical_input.tenant_id,
                    logical_input.input_id,
                    logical_input.source_id,
                    logical_input.source_revision_id,
                    logical_input.contract.contract_id,
                    logical_input.contract.version,
                    logical_input.contract.digest,
                ),
            )
            previous_input = connection.execute(
                """SELECT source_id,source_revision_id,contract_id,contract_version,
                          contract_digest FROM work_intake.logical_inputs
                   WHERE tenant_id=%s AND input_id=%s""",
                (logical_input.tenant_id, logical_input.input_id),
            ).fetchone()
            expected_input = (
                logical_input.source_id,
                logical_input.source_revision_id,
                logical_input.contract.contract_id,
                logical_input.contract.version,
                logical_input.contract.digest,
            )
            if (
                previous_input is None
                or tuple(previous_input.values()) != expected_input
            ):
                raise WorkIntakeError(
                    "logical Input identity conflicts with its revision"
                )
            connection.execute(
                """INSERT INTO work_intake.submissions
                   (tenant_id,submission_id,input_id,source_id,source_revision_id,
                    idempotency_scope,idempotency_key,
                    intent_digest,release_id,contract_id,contract_version,contract_digest,
                    parameters_json,parameters_digest,dispatch_operation_id,dispatch_state,
                    created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)
                   ON CONFLICT (tenant_id,idempotency_scope,idempotency_key) DO NOTHING""",
                (
                    intent.tenant_id,
                    UUID(intent.submission_id),
                    intent.input_id,
                    intent.source_id,
                    intent.source_revision_id,
                    intent.idempotency_scope,
                    intent.idempotency_key,
                    intent.intent_digest(),
                    intent.release_id,
                    intent.input_contract.contract_id,
                    intent.input_contract.version,
                    intent.input_contract.digest,
                    intent.parameters_json,
                    intent.parameters_digest,
                    intent.dispatch_operation_id,
                    intent.created_at,
                ),
            )
            row = connection.execute(
                """SELECT * FROM work_intake.submissions
                   WHERE tenant_id=%s AND idempotency_scope=%s AND idempotency_key=%s
                   FOR UPDATE""",
                (intent.tenant_id, intent.idempotency_scope, intent.idempotency_key),
            ).fetchone()
            if row is None or row["intent_digest"] != intent.intent_digest():
                raise WorkIntakeError(
                    "idempotency key was reused with conflicting intent"
                )
            return _submission(row)

    def get_submission(self, tenant_id: str, submission_id: str) -> Submission | None:
        try:
            identity = UUID(submission_id)
        except ValueError as error:
            raise WorkIntakeError("submission identity must be a UUID") from error
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM work_intake.submissions
                   WHERE tenant_id=%s AND submission_id=%s""",
                (tenant_id, identity),
            ).fetchone()
        return None if row is None else _submission(row)

    def get_by_idempotency(
        self, tenant_id: str, idempotency_scope: str, idempotency_key: str
    ) -> Submission | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM work_intake.submissions
                   WHERE tenant_id=%s AND idempotency_scope=%s AND idempotency_key=%s""",
                (tenant_id, idempotency_scope, idempotency_key),
            ).fetchone()
        return None if row is None else _submission(row)

    def claim_pending(
        self,
        *,
        limit: int,
        worker_id: str,
        lease_seconds: int,
        tenant_id: str | None = None,
    ) -> tuple[Submission, ...]:
        if not worker_id or len(worker_id) > 128:
            raise WorkIntakeError("dispatch worker identity is invalid")
        if tenant_id is not None and not re.fullmatch(
            r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$", tenant_id
        ):
            raise WorkIntakeError("dispatch tenant identity is invalid")
        with self._connect() as connection, connection.transaction():
            rows = connection.execute(
                """WITH candidates AS (
                       SELECT tenant_id,submission_id
                       FROM work_intake.submissions
                       WHERE (%s::text IS NULL OR tenant_id=%s)
                         AND (dispatch_state='pending'
                          OR (dispatch_state='claimed' AND claim_until <= clock_timestamp()))
                       ORDER BY created_at,submission_id
                       FOR UPDATE SKIP LOCKED
                       LIMIT %s
                   )
                   UPDATE work_intake.submissions AS submission
                   SET dispatch_state='claimed',claim_owner=%s,
                       claim_until=clock_timestamp() + (%s * interval '1 second'),
                       claim_generation=claim_generation+1,
                       updated_at=clock_timestamp()
                   FROM candidates
                   WHERE submission.tenant_id=candidates.tenant_id
                     AND submission.submission_id=candidates.submission_id
                   RETURNING submission.*""",
                (tenant_id, tenant_id, limit, worker_id, lease_seconds),
            ).fetchall()
        return tuple(_submission(row) for row in rows)

    def mark_admitted(
        self,
        tenant_id: str,
        submission_id: str,
        worker_id: str,
        claim_generation: int,
        run_id: str,
    ) -> Submission:
        try:
            submission_uuid = UUID(submission_id)
            run_uuid = UUID(run_id)
        except ValueError as error:
            raise WorkIntakeError(
                "run and submission identities must be UUIDs"
            ) from error
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE work_intake.submissions
                   SET dispatch_state='admitted',run_id=%s,claim_owner=NULL,
                       claim_until=NULL,updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND submission_id=%s AND dispatch_state='claimed'
                     AND claim_owner=%s AND claim_generation=%s
                   RETURNING *""",
                (run_uuid, tenant_id, submission_uuid, worker_id, claim_generation),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT * FROM work_intake.submissions
                       WHERE tenant_id=%s AND submission_id=%s FOR UPDATE""",
                    (tenant_id, submission_uuid),
                ).fetchone()
                if (
                    row is None
                    or row["dispatch_state"] != "admitted"
                    or row["run_id"] != run_uuid
                ):
                    raise WorkIntakeError(
                        "submission dispatch claim is stale or conflicting"
                    )
        return _submission(row)

    def mark_rejected(
        self,
        tenant_id: str,
        submission_id: str,
        worker_id: str,
        claim_generation: int,
        diagnostic_code: str,
    ) -> Submission:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", diagnostic_code):
            raise WorkIntakeError("dispatch diagnostic code is invalid")
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE work_intake.submissions
                   SET dispatch_state='rejected',diagnostic_code=%s,claim_owner=NULL,
                       claim_until=NULL,updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND submission_id=%s AND dispatch_state='claimed'
                     AND claim_owner=%s AND claim_generation=%s RETURNING *""",
                (
                    diagnostic_code,
                    tenant_id,
                    UUID(submission_id),
                    worker_id,
                    claim_generation,
                ),
            ).fetchone()
            if row is None:
                raise WorkIntakeError("submission dispatch claim is stale")
        return _submission(row)

    def release_claim(
        self, tenant_id: str, submission_id: str, worker_id: str, claim_generation: int
    ) -> None:
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """UPDATE work_intake.submissions
                   SET dispatch_state='pending',claim_owner=NULL,claim_until=NULL,
                       updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND submission_id=%s AND dispatch_state='claimed'
                     AND claim_owner=%s AND claim_generation=%s""",
                (tenant_id, UUID(submission_id), worker_id, claim_generation),
            )

    def cancel_pending(self, tenant_id: str, submission_id: str) -> Submission:
        try:
            identity = UUID(submission_id)
        except ValueError as error:
            raise WorkIntakeError("submission identity must be a UUID") from error
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE work_intake.submissions
                   SET dispatch_state='cancelled',updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND submission_id=%s AND dispatch_state='pending'
                   RETURNING *""",
                (tenant_id, identity),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT * FROM work_intake.submissions
                       WHERE tenant_id=%s AND submission_id=%s""",
                    (tenant_id, identity),
                ).fetchone()
                if row is None:
                    raise WorkIntakeError("submission does not exist")
                if row["dispatch_state"] != "cancelled":
                    raise WorkIntakeError(
                        "submission is no longer pending; cancellation belongs to BC04"
                    )
        return _submission(row)

    def attach_existing_admission(
        self, tenant_id: str, submission_id: str, run_id: str
    ) -> Submission:
        """Reconcile a BC04 receipt before cancellation of a retried intent."""
        try:
            identity = UUID(submission_id)
            run_identity = UUID(run_id)
        except ValueError as error:
            raise WorkIntakeError("submission or run identity is invalid") from error
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE work_intake.submissions
                   SET dispatch_state='admitted',run_id=%s,updated_at=clock_timestamp()
                   WHERE tenant_id=%s AND submission_id=%s AND dispatch_state='pending'
                   RETURNING *""",
                (run_identity, tenant_id, identity),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT * FROM work_intake.submissions
                       WHERE tenant_id=%s AND submission_id=%s""",
                    (tenant_id, identity),
                ).fetchone()
                if row is None:
                    raise WorkIntakeError("submission does not exist")
                if row["dispatch_state"] != "admitted" or row["run_id"] != run_identity:
                    raise WorkIntakeError(
                        "submission changed while resolving its admission receipt"
                    )
        return _submission(row)


def _submission(row: dict[str, Any]) -> Submission:
    reference = ContractReference(
        row["contract_id"], row["contract_version"], row["contract_digest"]
    )
    intent = SubmissionIntent(
        tenant_id=row["tenant_id"],
        submission_id=str(row["submission_id"]),
        input_id=row["input_id"],
        source_id=row["source_id"],
        source_revision_id=row["source_revision_id"],
        idempotency_scope=row["idempotency_scope"],
        idempotency_key=row["idempotency_key"],
        release_id=row["release_id"],
        input_contract=reference,
        parameters_json=bytes(row["parameters_json"]),
        parameters_digest=row["parameters_digest"],
        created_at=row["created_at"],
        dispatch_operation_id=row["dispatch_operation_id"],
    )
    if intent.intent_digest() != row["intent_digest"]:
        raise WorkIntakeError("persisted submission intent digest differs")
    return Submission(
        intent=intent,
        state=DispatchState(row["dispatch_state"]),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
        diagnostic_code=row["diagnostic_code"],
        claim_owner=row["claim_owner"],
        claim_generation=row["claim_generation"],
    )
