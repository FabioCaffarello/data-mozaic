"""Durable Scout session, command receipt, and handoff outbox adapter."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from ...domain import (
    ArtifactRef,
    AuthorizedNavigationScope,
    ConflictError,
    DiscoveryFinding,
    EvidenceManifest,
    IntegrityError,
    PlaybookProposal,
    ScoutObjective,
    ScoutScope,
    ScoutSession,
    SourceObservation,
    SourceRef,
    State,
)
from ...projections import projection

MIGRATIONS = Path(__file__).resolve().parents[5] / "migrations" / "scout"


def _session_document(session: ScoutSession) -> dict[str, Any]:
    return {"document_version": 1, "session": asdict(session)}


def _restore(value: dict[str, Any]) -> ScoutSession:
    if value.get("document_version") != 1 or not isinstance(value.get("session"), dict):
        raise IntegrityError("unsupported persisted Scout document version")
    raw = value["session"]
    source = SourceRef(**raw["source"])
    requested = ScoutScope(
        **{
            **raw["requested_scope"],
            "origins": tuple(raw["requested_scope"]["origins"]),
            "path_prefixes": tuple(raw["requested_scope"]["path_prefixes"]),
            "methods": tuple(raw["requested_scope"]["methods"]),
        }
    )
    authorization = raw["authorization"]
    authorized = None
    if authorization:
        s = authorization["scope"]
        authorized = AuthorizedNavigationScope(
            ScoutScope(
                tuple(s["origins"]),
                tuple(s["path_prefixes"]),
                tuple(s["methods"]),
                s["max_requests"],
                s["max_duration_seconds"],
            ),
            authorization["decision_id"],
        )
    observations = tuple(
        SourceObservation(
            item["observation_id"],
            item["session_id"],
            SourceRef(**item["source"]),
            item["statement"],
            item["artifact_id"],
        )
        for item in raw["observations"]
    )
    manifests = tuple(
        EvidenceManifest(
            item["manifest_id"],
            item["session_id"],
            SourceRef(**item["source"]),
            item["sealed_at"],
            tuple(
                ArtifactRef(**a | {"source": SourceRef(**a["source"])})
                for a in item["artifacts"]
            ),
        )
        for item in raw["manifests"]
    )
    findings = tuple(
        DiscoveryFinding(
            item["finding_id"],
            item["session_id"],
            SourceRef(**item["source"]),
            item["statement"],
            item["epistemic_status"],
            item["confidence"],
            tuple(item["manifest_ids"]),
            item["verification_note"],
        )
        for item in raw["findings"]
    )
    proposal_raw = raw["proposal"]
    proposal = (
        None
        if proposal_raw is None
        else PlaybookProposal(
            proposal_raw["proposal_id"],
            proposal_raw["session_id"],
            SourceRef(**proposal_raw["source"]),
            proposal_raw["summary"],
            tuple(proposal_raw["finding_ids"]),
            tuple(proposal_raw["manifest_ids"]),
            tuple(proposal_raw["assumptions"]),
            tuple(proposal_raw["risks"]),
            tuple(proposal_raw["required_capabilities"]),
        )
    )
    # Hydration is the sole adapter boundary allowed to reconstruct a terminal
    # snapshot. Build the identity first, then restore persisted state without
    # invoking public domain transitions or weakening ScoutSession.__setattr__.
    session = ScoutSession(
        raw["session_id"],
        source,
        ScoutObjective(**raw["objective"]),
        requested,
        raw["principal"],
    )
    for name, restored_value in (
        ("state", State(raw["state"])),
        ("version", raw["version"]),
        ("authorization", authorized),
        ("remaining_requests", raw["remaining_requests"]),
        ("remaining_seconds", raw["remaining_seconds"]),
        ("observations", observations),
        ("manifests", manifests),
        ("findings", findings),
        ("proposal", proposal),
        ("handoff_result", raw["handoff_result"]),
    ):
        object.__setattr__(session, name, restored_value)
    return session


class PostgresScoutSessionRepository:
    """PostgreSQL implementation using READ COMMITTED and explicit SQL transactions."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def migrate(self) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (1413769806,))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scout_schema_migrations (migration_name text PRIMARY KEY, checksum text, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            conn.execute(
                "ALTER TABLE scout_schema_migrations ADD COLUMN IF NOT EXISTS checksum text"
            )
            for path in sorted(MIGRATIONS.glob("*.sql")):
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                present = conn.execute(
                    "SELECT checksum FROM scout_schema_migrations WHERE migration_name=%s",
                    (path.name,),
                ).fetchone()
                if present:
                    if present[0] != checksum:
                        raise IntegrityError(
                            f"migration checksum mismatch or legacy checksum missing: {path.name}"
                        )
                    continue
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO scout_schema_migrations(migration_name,checksum) VALUES (%s,%s)",
                    (path.name, checksum),
                )

    def find_intent(self, principal: str, key: str, digest: str) -> ScoutSession | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """SELECT r.fingerprint, s.payload FROM scout_command_receipts r
                JOIN scout_sessions s USING (session_id) WHERE r.principal=%s AND r.idempotency_key=%s""",
                (principal, key),
            ).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise ConflictError("idempotency key reused with different arguments")
        return _restore(row[1])

    def find_intent_header(
        self, principal: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        """Read only replay authorization metadata, never the sensitive receipt."""
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """SELECT r.fingerprint,r.principal,s.payload #> '{session,source}'
                FROM scout_command_receipts r JOIN scout_sessions s USING (session_id)
                WHERE r.principal=%s AND r.idempotency_key=%s""",
                (principal, key),
            ).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise ConflictError("idempotency key reused with different arguments")
        return {"principal": row[1], "source": SourceRef(**row[2])}

    def create(self, session: ScoutSession, key: str, digest: str) -> None:
        document = _session_document(session)
        with psycopg.connect(self.dsn) as conn:
            try:
                conn.execute(
                    "INSERT INTO scout_sessions(session_id,version,document_version,payload) VALUES (%s,%s,1,%s)",
                    (session.session_id, session.version, Jsonb(document)),
                )
                conn.execute(
                    """INSERT INTO scout_command_receipts
                    (principal,idempotency_key,command_id,command_kind,fingerprint,session_id,status,response_snapshot)
                    VALUES (%s,%s,%s,'RunScout',%s,%s,'in_progress',%s)""",
                    (
                        session.principal,
                        key,
                        session.session_id,
                        digest,
                        session.session_id,
                        Jsonb(document),
                    ),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ConflictError("session or command intent already exists") from exc

    def save(self, session: ScoutSession, expected_version: int) -> None:
        next_version = expected_version + 1
        document = _session_document(session)
        document["session"]["version"] = next_version
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """UPDATE scout_sessions SET version=%s,payload=%s,updated_at=now()
                WHERE session_id=%s AND version=%s RETURNING session_id""",
                (next_version, Jsonb(document), session.session_id, expected_version),
            ).fetchone()
            if row is None:
                raise ConflictError("optimistic concurrency conflict")
            receipt_status = (
                "completed" if session.state == State.COMPLETED else "in_progress"
            )
            conn.execute(
                "UPDATE scout_command_receipts SET status=%s,response_snapshot=%s,updated_at=now() WHERE session_id=%s",
                (receipt_status, Jsonb(document), session.session_id),
            )
            if session.proposal is not None:
                payload = projection(session.proposal)
                conn.execute(
                    """INSERT INTO scout_outbox
                    (event_id,aggregate_id,aggregate_version,event_kind,channel,proposal_id,payload,publication_state)
                    VALUES (%s,%s,%s,'PlaybookProposalSubmitted','BC15',%s,%s,'pending')
                    ON CONFLICT (channel,proposal_id) DO NOTHING""",
                    (
                        "handoff:" + session.proposal.proposal_id,
                        session.session_id,
                        next_version,
                        session.proposal.proposal_id,
                        Jsonb(payload),
                    ),
                )
        session.version = next_version

    def pending_handoffs(self, limit: int = 100) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn) as conn:
            rows = conn.execute(
                """SELECT event_id,proposal_id,payload,publication_state,attempt_count
                FROM scout_outbox WHERE (
                    publication_state IN ('pending','uncertain','retry_scheduled')
                    AND next_attempt_at <= clock_timestamp()
                ) OR (publication_state='leased' AND lease_deadline <= clock_timestamp())
                ORDER BY created_at LIMIT %s""",
                (limit,),
            ).fetchall()
        return [
            dict(
                zip(
                    (
                        "event_id",
                        "proposal_id",
                        "payload",
                        "publication_state",
                        "attempt_count",
                    ),
                    row,
                )
            )
            for row in rows
        ]

    def handoff_status(self, event_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT publication_state,business_ack,attempt_count FROM scout_outbox WHERE event_id=%s",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "publication_state": row[0],
            "business_ack": row[1],
            "attempt_count": row[2],
        }

    def claim_handoff(
        self, owner: str, lease_seconds: int, now
    ) -> dict[str, Any] | None:
        """Claim one row atomically; DB clock is authoritative for lease validity."""
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """WITH candidate AS (
                    SELECT event_id FROM scout_outbox
                    WHERE ((publication_state IN ('pending','uncertain','retry_scheduled')
                            AND next_attempt_at <= clock_timestamp())
                        OR (publication_state = 'leased' AND lease_deadline <= clock_timestamp()))
                    ORDER BY next_attempt_at, created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE scout_outbox AS o SET publication_state='leased',
                    lease_owner=%s,
                    lease_deadline=clock_timestamp() + (%s * interval '1 second'),
                    fencing_token=o.fencing_token+1,
                    attempt_count=o.attempt_count+1,
                    last_result='claimed'
                FROM candidate c WHERE o.event_id=c.event_id
                RETURNING o.event_id,o.payload,o.attempt_count,o.fencing_token""",
                (owner, lease_seconds),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(("event_id", "payload", "attempt_count", "fencing_token"), row))

    def finish_handoff(
        self,
        event_id: str,
        owner: str,
        fencing_token: int,
        *,
        status: str,
        error_class: str | None,
        result: str,
        next_attempt_delay_seconds: int,
        receipt: dict | None = None,
    ) -> bool:
        if status not in {"sent", "retry_scheduled", "dead_letter"}:
            raise ValueError("invalid relay completion status")
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """UPDATE scout_outbox SET publication_state=%s,lease_owner=NULL,
                    lease_deadline=NULL,error_class=%s,last_result=%s,
                    next_attempt_at=clock_timestamp() + (%s * interval '1 second'),transport_receipt=%s,
                    business_ack=CASE WHEN %s='sent' THEN %s ELSE business_ack END
                WHERE event_id=%s AND publication_state='leased'
                    AND lease_owner=%s AND fencing_token=%s
                RETURNING event_id""",
                (
                    status,
                    error_class,
                    result,
                    next_attempt_delay_seconds,
                    Jsonb(receipt) if receipt is not None else None,
                    status,
                    Jsonb(receipt) if receipt is not None else None,
                    event_id,
                    owner,
                    fencing_token,
                ),
            ).fetchone()
        return row is not None

    def begin_custody_intent(
        self,
        artifact_id: str,
        session_id: str,
        sha256: str,
        size_bytes: int | None = None,
    ) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                """INSERT INTO scout_custody_intents(artifact_id,session_id,sha256,status,expected_size_bytes)
                VALUES (%s,%s,%s,'pending',%s) ON CONFLICT (artifact_id) DO UPDATE
                SET expected_size_bytes=COALESCE(scout_custody_intents.expected_size_bytes,EXCLUDED.expected_size_bytes)""",
                (artifact_id, session_id, sha256, size_bytes),
            )
            row = conn.execute(
                "SELECT session_id,sha256,expected_size_bytes FROM scout_custody_intents WHERE artifact_id=%s",
                (artifact_id,),
            ).fetchone()
            if row[:2] != (session_id, sha256) or (
                size_bytes is not None and row[2] not in (None, size_bytes)
            ):
                raise ConflictError(
                    "custody effect identity reused with different content"
                )

    def mark_custody_unknown(self, artifact_id: str) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                """UPDATE scout_custody_intents SET status='unknown',updated_at=now()
                WHERE artifact_id=%s AND status='pending'""",
                (artifact_id,),
            )

    def get_custody_intent(self, artifact_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "SELECT i.artifact_id,i.session_id,i.sha256,i.status,i.artifact_ref,s.payload #> '{session,source}',i.expected_size_bytes FROM scout_custody_intents i JOIN scout_sessions s USING (session_id) WHERE i.artifact_id=%s",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(
            zip(
                (
                    "artifact_id",
                    "session_id",
                    "sha256",
                    "status",
                    "artifact_ref",
                    "source",
                    "expected_size_bytes",
                ),
                row,
            )
        )
        result["source"] = SourceRef(**result["source"])
        return result

    def mark_custody_discrepancy(self, artifact_id: str, note: str) -> None:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                "UPDATE scout_custody_intents SET status='manual_review',reconciliation_note=%s,updated_at=now() WHERE artifact_id=%s AND status IN ('pending','unknown','discrepancy','manual_review') RETURNING session_id",
                (note[:240], artifact_id),
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO scout_reconciliation_events(event_id,artifact_id,session_id,event_kind,owner,details) VALUES (%s,%s,%s,'digest_or_identity_discrepancy','BC14',%s)",
                    (
                        "custody-discrepancy:" + uuid4().hex,
                        artifact_id,
                        row[0],
                        Jsonb(
                            {"classification": "manual_review", "reason": note[:120]}
                        ),
                    ),
                )

    def record_orphan_review(
        self, review_id: str, artifact_id: str, diagnostic: dict
    ) -> None:
        allowed = {
            "identity",
            "sha256",
            "size_bytes",
            "custody_status",
            "source_id",
            "revision",
        }
        if set(diagnostic) - allowed:
            raise IntegrityError("orphan diagnostic contains non-allowlisted fields")
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                "INSERT INTO scout_orphan_reviews(review_id,artifact_id,reported_by,status,diagnostic) VALUES (%s,%s,'BC06','manual_review',%s) ON CONFLICT (review_id) DO NOTHING",
                (review_id, artifact_id, Jsonb(diagnostic)),
            )
            conn.execute(
                "INSERT INTO scout_reconciliation_events(event_id,artifact_id,event_kind,owner,details) VALUES (%s,%s,'orphan_candidate','BC06',%s) ON CONFLICT (event_id) DO NOTHING",
                (
                    "orphan-review:" + review_id,
                    artifact_id,
                    Jsonb({"classification": "manual_review", "review_id": review_id}),
                ),
            )

    def confirm_custody_intent(
        self, artifact_id: str, sha256: str, artifact_ref: dict
    ) -> None:
        if (
            artifact_ref.get("artifact_id") != artifact_id
            or artifact_ref.get("sha256") != sha256
        ):
            raise IntegrityError("custody receipt identity or digest mismatch")
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute(
                """UPDATE scout_custody_intents SET status='verified',artifact_ref=%s,updated_at=now()
                WHERE artifact_id=%s AND sha256=%s AND status IN ('pending','unknown','verified')
                RETURNING artifact_id""",
                (Jsonb(artifact_ref), artifact_id, sha256),
            ).fetchone()
            if row is None:
                raise IntegrityError("custody intent not found or digest changed")
