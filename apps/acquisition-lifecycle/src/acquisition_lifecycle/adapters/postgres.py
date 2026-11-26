"""PostgreSQL adapters over owner-specific BC01, BC11 and BC15 schemas."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..domain import LifecycleError


class PostgresLifecycleStore:
    """Application composition adapter; each context owns its own tables."""

    def __init__(self, dsn: str, migrations_root: Path, *, connect_timeout: int = 5):
        if not dsn or not 1 <= connect_timeout <= 30:
            raise LifecycleError("lifecycle PostgreSQL configuration is invalid")
        self._dsn = dsn
        self._migrations_root = migrations_root.resolve(strict=True)
        self._connect_timeout = connect_timeout

    def _connect(self):
        return psycopg.connect(
            self._dsn, connect_timeout=self._connect_timeout, row_factory=dict_row
        )

    def migrate(self) -> None:
        migrations = (
            self._migrations_root / "source-management" / "001-source-revisions.sql",
            self._migrations_root / "acquisition-policy" / "001-policy-revisions.sql",
            self._migrations_root
            / "collector-engineering"
            / "001-release-lifecycle.sql",
            self._migrations_root
            / "collector-engineering"
            / "002-build-runtime-bindings.sql",
            self._migrations_root
            / "collector-engineering"
            / "003-remove-runtime-from-build-content.sql",
            self._migrations_root
            / "collector-engineering"
            / "004-authority-publication-recovery.sql",
        )
        with self._connect() as connection, connection.transaction():
            connection.execute("CREATE SCHEMA IF NOT EXISTS lifecycle_control")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS lifecycle_control.schema_migrations (
                    version text PRIMARY KEY,
                    checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
                )"""
            )
        for migration in migrations:
            sql = migration.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            with self._connect() as connection, connection.transaction():
                previous = connection.execute(
                    "SELECT checksum FROM lifecycle_control.schema_migrations WHERE version=%s FOR UPDATE",
                    (migration.parent.name + "/" + migration.stem,),
                ).fetchone()
                if previous is not None:
                    if previous["checksum"] != checksum:
                        raise LifecycleError("lifecycle migration checksum changed")
                    continue
                connection.execute(sql)
                connection.execute(
                    "INSERT INTO lifecycle_control.schema_migrations(version,checksum) VALUES(%s,%s)",
                    (migration.parent.name + "/" + migration.stem, checksum),
                )

    def grant_runtime_role(self, role_name: str) -> None:
        if (
            not role_name
            or len(role_name) > 63
            or not role_name.replace("_", "").isalnum()
        ):
            raise LifecycleError("lifecycle runtime role name is invalid")
        grants = {
            "SELECT, INSERT": (
                "source_management.sources",
                "source_management.source_revisions",
                "source_management.source_revision_events",
                "acquisition_policy.policy_revisions",
                "acquisition_policy.policy_revision_events",
                "collector_engineering.playbook_revisions",
                "collector_engineering.bot_builds",
                "collector_engineering.verified_bot_builds",
                "collector_engineering.release_events",
            ),
            "SELECT, INSERT, UPDATE": (
                "source_management.source_revision_eligibility",
                "acquisition_policy.policy_eligibility",
                "collector_engineering.bot_releases",
                "collector_engineering.authority_publications",
            ),
            "SELECT": ("lifecycle_control.schema_migrations",),
        }
        with self._connect() as connection, connection.transaction():
            grantee = sql.Identifier(role_name)
            for schema in (
                "source_management",
                "acquisition_policy",
                "collector_engineering",
                "lifecycle_control",
            ):
                connection.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema), grantee
                    )
                )
            for privileges, tables in grants.items():
                connection.execute(
                    sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                        sql.SQL(privileges),
                        sql.SQL(", ").join(
                            sql.Identifier(*table.split(".")) for table in tables
                        ),
                        grantee,
                    )
                )

    def register_source(self, document: dict[str, Any], actor_id: str) -> str:
        source_id = document["source_id"]
        revision_id = document["revision_id"]
        digest = _document_digest(document)
        with self._connect() as connection, connection.transaction():
            connection.execute(
                "INSERT INTO source_management.sources(source_id,owner) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (source_id, document["owner"]),
            )
            source = connection.execute(
                "SELECT owner FROM source_management.sources WHERE source_id=%s FOR UPDATE",
                (source_id,),
            ).fetchone()
            if source is None or source["owner"] != document["owner"]:
                raise LifecycleError("source identity conflicts with registered owner")
            connection.execute(
                """INSERT INTO source_management.source_revisions
                (source_id,revision_id,revision_digest,document,registered_by)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (source_id, revision_id, digest, Jsonb(document), actor_id),
            )
            existing = connection.execute(
                """SELECT revision_digest FROM source_management.source_revisions
                WHERE source_id=%s AND revision_id=%s FOR UPDATE""",
                (source_id, revision_id),
            ).fetchone()
            if existing is None or existing["revision_digest"] != digest:
                raise LifecycleError(
                    "source revision identity conflicts with immutable content"
                )
            connection.execute(
                """INSERT INTO source_management.source_revision_eligibility
                (source_id,revision_id,state,changed_by) VALUES(%s,%s,'eligible',%s)
                ON CONFLICT DO NOTHING""",
                (source_id, revision_id, actor_id),
            )
            connection.execute(
                """INSERT INTO source_management.source_revision_events
                (event_id,source_id,revision_id,event_type,actor_id)
                SELECT %s,%s,%s,'registered',%s
                WHERE NOT EXISTS (SELECT 1 FROM source_management.source_revision_events
                    WHERE source_id=%s AND revision_id=%s AND event_type='registered')""",
                (
                    str(uuid4()),
                    source_id,
                    revision_id,
                    actor_id,
                    source_id,
                    revision_id,
                ),
            )
        return digest

    def resolve_source(self, source_id: str, revision_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT r.document, r.revision_digest, e.state
                FROM source_management.source_revisions r
                JOIN source_management.source_revision_eligibility e USING(source_id,revision_id)
                WHERE r.source_id=%s AND r.revision_id=%s""",
                (source_id, revision_id),
            ).fetchone()
        if row is None:
            raise LifecycleError("source revision is unresolved")
        document = dict(row["document"])
        document["status"] = row["state"]
        document["configuration_digest"] = _document_digest(
            document, "configuration_digest"
        )
        document["_revision_digest"] = row["revision_digest"]
        return document

    def set_source_state(
        self, source_id: str, revision_id: str, state: str, actor_id: str
    ) -> None:
        if state not in {"eligible", "revoked"}:
            raise LifecycleError("source eligibility state is invalid")
        event_type = "reinstated" if state == "eligible" else "revoked"
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE source_management.source_revision_eligibility
                SET state=%s,changed_by=%s,changed_at=clock_timestamp()
                WHERE source_id=%s AND revision_id=%s AND state<>%s RETURNING source_id""",
                (state, actor_id, source_id, revision_id, state),
            ).fetchone()
            if row is None:
                found = connection.execute(
                    "SELECT state FROM source_management.source_revision_eligibility WHERE source_id=%s AND revision_id=%s",
                    (source_id, revision_id),
                ).fetchone()
                if found is None:
                    raise LifecycleError("source revision is unresolved")
                return
            connection.execute(
                "INSERT INTO source_management.source_revision_events(event_id,source_id,revision_id,event_type,actor_id) VALUES(%s,%s,%s,%s,%s)",
                (str(uuid4()), source_id, revision_id, event_type, actor_id),
            )

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT r.document,e.state FROM source_management.source_revisions r
                JOIN source_management.source_revision_eligibility e USING(source_id,revision_id)
                ORDER BY r.source_id,r.revision_id"""
            ).fetchall()
        result = []
        for row in rows:
            document = dict(row["document"])
            document["status"] = row["state"]
            document["configuration_digest"] = _document_digest(
                document, "configuration_digest"
            )
            result.append(document)
        return result

    def register_policy(self, document: dict[str, Any], actor_id: str) -> str:
        digest = _document_digest(document, "policy_digest")
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """INSERT INTO acquisition_policy.policy_revisions
                (policy_id,policy_version,policy_digest,document,registered_by)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    document["policy_id"],
                    document["policy_version"],
                    digest,
                    Jsonb(document),
                    actor_id,
                ),
            )
            existing = connection.execute(
                """SELECT policy_digest FROM acquisition_policy.policy_revisions
                WHERE policy_id=%s AND policy_version=%s FOR UPDATE""",
                (document["policy_id"], document["policy_version"]),
            ).fetchone()
            if existing is None or existing["policy_digest"] != digest:
                raise LifecycleError(
                    "policy identity/version conflicts with immutable content"
                )
            connection.execute(
                """INSERT INTO acquisition_policy.policy_eligibility
                (policy_id,policy_version,state,changed_by) VALUES(%s,%s,'eligible',%s)
                ON CONFLICT DO NOTHING""",
                (document["policy_id"], document["policy_version"], actor_id),
            )
            connection.execute(
                """INSERT INTO acquisition_policy.policy_revision_events
                (event_id,policy_id,policy_version,event_type,actor_id)
                SELECT %s,%s,%s,'registered',%s WHERE NOT EXISTS (
                    SELECT 1 FROM acquisition_policy.policy_revision_events
                    WHERE policy_id=%s AND policy_version=%s AND event_type='registered')""",
                (
                    str(uuid4()),
                    document["policy_id"],
                    document["policy_version"],
                    actor_id,
                    document["policy_id"],
                    document["policy_version"],
                ),
            )
        return digest

    def resolve_policy(self, policy_id: str, version: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT r.document,r.policy_digest,e.state FROM acquisition_policy.policy_revisions r
                JOIN acquisition_policy.policy_eligibility e USING(policy_id,policy_version)
                WHERE r.policy_id=%s AND r.policy_version=%s""",
                (policy_id, version),
            ).fetchone()
        if row is None:
            raise LifecycleError("policy revision is unresolved")
        document = dict(row["document"])
        document["status"] = row["state"]
        document["policy_digest"] = _document_digest(document, "policy_digest")
        document["_revision_digest"] = row["policy_digest"]
        return document

    def set_policy_state(
        self, policy_id: str, version: str, state: str, actor_id: str
    ) -> None:
        if state not in {"eligible", "revoked"}:
            raise LifecycleError("policy eligibility state is invalid")
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE acquisition_policy.policy_eligibility
                SET state=%s,changed_by=%s,changed_at=clock_timestamp()
                WHERE policy_id=%s AND policy_version=%s AND state<>%s RETURNING policy_id""",
                (state, actor_id, policy_id, version, state),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT state FROM acquisition_policy.policy_eligibility WHERE policy_id=%s AND policy_version=%s",
                    (policy_id, version),
                ).fetchone()
                if exists is None:
                    raise LifecycleError("policy revision is unresolved")
            else:
                connection.execute(
                    "INSERT INTO acquisition_policy.policy_revision_events(event_id,policy_id,policy_version,event_type,actor_id) VALUES(%s,%s,%s,%s,%s)",
                    (
                        str(uuid4()),
                        policy_id,
                        version,
                        "reinstated" if state == "eligible" else "revoked",
                        actor_id,
                    ),
                )

    def list_policies(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT r.document,e.state FROM acquisition_policy.policy_revisions r
                JOIN acquisition_policy.policy_eligibility e USING(policy_id,policy_version)
                ORDER BY r.policy_id,r.policy_version"""
            ).fetchall()
        result = []
        for row in rows:
            document = dict(row["document"])
            document["status"] = row["state"]
            document["policy_digest"] = _document_digest(document, "policy_digest")
            result.append(document)
        return result

    def register_playbook(
        self,
        playbook_id: str,
        digest: str,
        source_id: str,
        revision_id: str,
        content: bytes,
        actor_id: str,
    ) -> None:
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """INSERT INTO collector_engineering.playbook_revisions
                (playbook_id,playbook_digest,source_id,source_revision_id,content,registered_by)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (playbook_id, digest, source_id, revision_id, content, actor_id),
            )
            existing = connection.execute(
                """SELECT source_id,source_revision_id,content FROM collector_engineering.playbook_revisions
                WHERE playbook_id=%s AND playbook_digest=%s FOR UPDATE""",
                (playbook_id, digest),
            ).fetchone()
            if (
                existing is None
                or existing["source_id"] != source_id
                or existing["source_revision_id"] != revision_id
                or bytes(existing["content"]) != content
            ):
                raise LifecycleError(
                    "Playbook revision identity conflicts with immutable content"
                )

    def resolve_playbook(self, playbook_id: str, digest: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT source_id,source_revision_id,content,registered_by,registered_at
                FROM collector_engineering.playbook_revisions WHERE playbook_id=%s AND playbook_digest=%s""",
                (playbook_id, digest),
            ).fetchone()
        if row is None:
            raise LifecycleError("Playbook revision is unresolved")
        return {**dict(row), "content": bytes(row["content"])}

    def register_build(
        self,
        build_id: str,
        manifest_digest: str,
        runtime_digest: str,
        path: Path,
        manifest: dict[str, Any],
        actor_id: str,
    ) -> None:
        artifact_path = path.resolve(strict=True)
        if path.is_symlink() or not artifact_path.is_dir():
            raise LifecycleError("BotBuild artifact path is not a regular directory")
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """INSERT INTO collector_engineering.bot_builds
                (build_id,manifest_digest,artifact_path,manifest,registered_by)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    build_id,
                    manifest_digest,
                    str(artifact_path),
                    Jsonb(manifest),
                    actor_id,
                ),
            )
            existing = connection.execute(
                """SELECT manifest_digest,artifact_path,manifest FROM collector_engineering.bot_builds
                WHERE build_id=%s FOR UPDATE""",
                (build_id,),
            ).fetchone()
            if (
                existing is None
                or existing["manifest_digest"] != manifest_digest
                or existing["artifact_path"] != str(artifact_path)
                or dict(existing["manifest"]) != manifest
            ):
                raise LifecycleError(
                    "BotBuild identity conflicts with immutable verified content"
                )
            connection.execute(
                """INSERT INTO collector_engineering.verified_bot_builds
                (build_id,runtime_image_digest,registered_by) VALUES(%s,%s,%s)
                ON CONFLICT DO NOTHING""",
                (build_id, runtime_digest, actor_id),
            )
            binding = connection.execute(
                """SELECT 1 FROM collector_engineering.verified_bot_builds
                WHERE build_id=%s AND runtime_image_digest=%s FOR UPDATE""",
                (build_id, runtime_digest),
            ).fetchone()
            if binding is None:
                raise LifecycleError("BotBuild runtime binding could not be registered")

    def resolve_build(self, build_id: str, runtime_image_digest: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT b.manifest_digest,v.runtime_image_digest,b.artifact_path,b.manifest
                FROM collector_engineering.verified_bot_builds v
                JOIN collector_engineering.bot_builds b USING(build_id)
                WHERE v.build_id=%s AND v.runtime_image_digest=%s""",
                (build_id, runtime_image_digest),
            ).fetchone()
        if row is None:
            raise LifecycleError("BotBuild is unavailable")
        return {
            **dict(row),
            "manifest": dict(row["manifest"]),
            "artifact_path": Path(row["artifact_path"]),
        }

    def create_release(
        self, release_id: str, candidate: dict[str, Any], digest: str, actor_id: str
    ) -> None:
        with self._connect() as connection, connection.transaction():
            connection.execute(
                """INSERT INTO collector_engineering.bot_releases
                (release_id,candidate_digest,candidate,state,registered_by)
                VALUES(%s,%s,%s,'pending',%s) ON CONFLICT DO NOTHING""",
                (release_id, digest, Jsonb(candidate), actor_id),
            )
            existing = connection.execute(
                "SELECT candidate_digest FROM collector_engineering.bot_releases WHERE release_id=%s FOR UPDATE",
                (release_id,),
            ).fetchone()
            if existing is None or existing["candidate_digest"] != digest:
                raise LifecycleError(
                    "release identity conflicts with immutable candidate"
                )
            connection.execute(
                """INSERT INTO collector_engineering.release_events
                (event_id,release_id,event_type,actor_id,evidence_ref)
                SELECT %s,%s,'candidate-created',%s,'candidate://registered'
                WHERE NOT EXISTS (SELECT 1 FROM collector_engineering.release_events
                    WHERE release_id=%s AND event_type='candidate-created')""",
                (str(uuid4()), release_id, actor_id, release_id),
            )

    def approve_release(
        self,
        release_id: str,
        document: dict[str, Any],
        actor_id: str,
        evidence_ref: str,
    ) -> None:
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE collector_engineering.bot_releases SET state='approved',approved_release=%s,
                approved_by=%s,approved_at=clock_timestamp() WHERE release_id=%s AND state='pending'
                RETURNING release_id""",
                (Jsonb(document), actor_id, release_id),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT state,approved_release,approved_by FROM collector_engineering.bot_releases WHERE release_id=%s FOR UPDATE",
                    (release_id,),
                ).fetchone()
                if existing is None:
                    raise LifecycleError("release candidate is unresolved")
                if (
                    existing["state"] == "approved"
                    and dict(existing["approved_release"]) == document
                    and existing["approved_by"] == actor_id
                ):
                    return
                raise LifecycleError("release is not pending approval")
            connection.execute(
                "INSERT INTO collector_engineering.release_events(event_id,release_id,event_type,actor_id,evidence_ref) VALUES(%s,%s,'approved',%s,%s)",
                (str(uuid4()), release_id, actor_id, evidence_ref),
            )

    def revoke_release(self, release_id: str, actor_id: str, evidence_ref: str) -> None:
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE collector_engineering.bot_releases SET state='revoked',revoked_by=%s,revoked_at=clock_timestamp()
                WHERE release_id=%s AND state='approved' RETURNING release_id""",
                (actor_id, release_id),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT state FROM collector_engineering.bot_releases WHERE release_id=%s FOR UPDATE",
                    (release_id,),
                ).fetchone()
                if existing is None:
                    raise LifecycleError("release is unresolved")
                if existing["state"] == "revoked":
                    return
                raise LifecycleError("only an approved release can be revoked")
            connection.execute(
                "INSERT INTO collector_engineering.release_events(event_id,release_id,event_type,actor_id,evidence_ref) VALUES(%s,%s,'revoked',%s,%s)",
                (str(uuid4()), release_id, actor_id, evidence_ref),
            )

    def resolve_release(
        self, release_id: str, *, require_eligible: bool = True
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT candidate_digest,candidate,state,approved_release,approved_by,approved_at FROM collector_engineering.bot_releases WHERE release_id=%s",
                (release_id,),
            ).fetchone()
        if row is None:
            raise LifecycleError("BotRelease is unresolved")
        if row["state"] == "pending":
            return {
                "state": "pending",
                "candidate_digest": row["candidate_digest"],
                "candidate": dict(row["candidate"]),
            }
        if require_eligible and row["state"] != "approved":
            raise LifecycleError("BotRelease is not eligible for new execution")
        return {
            "state": row["state"],
            "candidate_digest": row["candidate_digest"],
            "candidate": dict(row["candidate"]),
            "approved_release": dict(row["approved_release"]),
            "approved_by": row["approved_by"],
            "approved_at": row["approved_at"],
        }

    def list_releases(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT release_id,state,approved_release FROM collector_engineering.bot_releases WHERE state IN ('approved','revoked') ORDER BY release_id"
            ).fetchall()
        return [
            {
                "release_id": row["release_id"],
                "state": row["state"],
                "document": dict(row["approved_release"]),
            }
            for row in rows
        ]

    def prepare_authority(
        self,
        authority_id: str,
        generation: int,
        snapshot_digest: str,
        envelope_digest: str,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        with self._connect() as connection, connection.transaction():
            pending = connection.execute(
                "SELECT generation,snapshot_digest,envelope_digest,signed_envelope FROM collector_engineering.authority_publications WHERE authority_id=%s AND state='pending' FOR UPDATE",
                (authority_id,),
            ).fetchone()
            if pending is not None:
                if (
                    pending["generation"] != generation
                    or pending["snapshot_digest"] != snapshot_digest
                    or pending["envelope_digest"] != envelope_digest
                ):
                    raise LifecycleError(
                        "an earlier authority publication must be reconciled first"
                    )
                return dict(pending)
            connection.execute(
                """INSERT INTO collector_engineering.authority_publications
                (authority_id,generation,snapshot_digest,envelope_digest,signed_envelope,state)
                VALUES(%s,%s,%s,%s,%s,'pending') ON CONFLICT DO NOTHING""",
                (
                    authority_id,
                    generation,
                    snapshot_digest,
                    envelope_digest,
                    Jsonb(envelope),
                ),
            )
            row = connection.execute(
                "SELECT generation,snapshot_digest,envelope_digest,signed_envelope,state FROM collector_engineering.authority_publications WHERE authority_id=%s AND generation=%s FOR UPDATE",
                (authority_id, generation),
            ).fetchone()
            if (
                row is None
                or row["snapshot_digest"] != snapshot_digest
                or row["envelope_digest"] != envelope_digest
            ):
                raise LifecycleError(
                    "authority generation conflicts with an existing publication"
                )
            return dict(row)

    def pending_authority(self, authority_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generation,snapshot_digest,envelope_digest,signed_envelope,state FROM collector_engineering.authority_publications WHERE authority_id=%s AND state='pending' ORDER BY generation LIMIT 1",
                (authority_id,),
            ).fetchone()
        return dict(row) if row else None

    def latest_authority_generation(self, authority_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT max(generation) AS generation FROM collector_engineering.authority_publications WHERE authority_id=%s",
                (authority_id,),
            ).fetchone()
        return int(row["generation"]) if row and row["generation"] is not None else 0

    def supersede_expired_authority(
        self, authority_id: str, generation: int, envelope_digest: str
    ) -> None:
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                """UPDATE collector_engineering.authority_publications
                SET state='superseded',superseded_at=clock_timestamp()
                WHERE authority_id=%s AND generation=%s AND state='pending'
                  AND envelope_digest=%s RETURNING generation""",
                (authority_id, generation, envelope_digest),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT state,envelope_digest FROM collector_engineering.authority_publications WHERE authority_id=%s AND generation=%s",
                    (authority_id, generation),
                ).fetchone()
                if (
                    existing is None
                    or existing["state"] != "superseded"
                    or existing["envelope_digest"] != envelope_digest
                ):
                    raise LifecycleError(
                        "expired authority publication could not be superseded safely"
                    )

    def mark_authority_installed(self, authority_id: str, generation: int) -> None:
        with self._connect() as connection, connection.transaction():
            row = connection.execute(
                "UPDATE collector_engineering.authority_publications SET state='installed',installed_at=clock_timestamp() WHERE authority_id=%s AND generation=%s AND state='pending' RETURNING generation",
                (authority_id, generation),
            ).fetchone()
            if row is None:
                existing = connection.execute(
                    "SELECT state FROM collector_engineering.authority_publications WHERE authority_id=%s AND generation=%s",
                    (authority_id, generation),
                ).fetchone()
                if existing is None or existing["state"] != "installed":
                    raise LifecycleError(
                        "authority publication state cannot be reconciled"
                    )


def _document_digest(document: dict[str, Any], field: str = "") -> str:
    import rfc8785

    value = (
        {key: item for key, item in document.items() if key != field}
        if field
        else document
    )
    return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()
