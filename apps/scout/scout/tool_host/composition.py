"""Local development composition; never a production BC01/BC06/BC11 adapter."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ..adapters.fixtures import FixtureExplorer
from ..adapters.local_custody import LocalFileEvidenceCustody
from ..adapters.postgres.repository import PostgresScoutSessionRepository
from ..application import ScoutApplication
from ..domain import AuthorizationError, PolicyDecisionRef, ScoutScope, SourceRef
from ..tools.policy import LocalAuthorizationStore
from .server import create_server


class _LocalSourceLookup:
    def __init__(self, policy: LocalAuthorizationStore):
        self.policy = policy

    def resolve(self, source: SourceRef) -> ScoutScope:
        registered = self.policy.registered_source(source)
        if registered is None or len(registered.grants) != 1:
            raise AuthorizationError("source unavailable or unsupported scope shape")
        grant = registered.grants[0]
        return ScoutScope(
            (grant.origin,),
            grant.path_prefixes,
            ("GET",),
            registered.max_requests,
            registered.max_duration_seconds,
        )


class _LocalPolicyDecision:
    def __init__(self, policy: LocalAuthorizationStore):
        self.policy = policy

    def decide(self, principal: str, source: SourceRef, now: datetime):
        effective = self.policy.authorize(principal, source, now)
        if len(effective.grants) != 1:
            raise AuthorizationError("source unavailable or unsupported scope shape")
        grant = effective.grants[0]
        registered = self.policy.approved_scope(principal, source)
        if registered is None:
            raise AuthorizationError("approval missing")
        return PolicyDecisionRef(
            decision_id=f"local-approval:{source.source_id}:{source.revision}:{principal}",
            principal=principal,
            source=source,
            allowed=True,
            expires_at=effective.expires_at,
            scope=ScoutScope(
                (grant.origin,),
                grant.path_prefixes,
                ("GET",),
                effective.max_requests,
                effective.max_duration_seconds,
            ),
            max_response_bytes=effective.max_response_bytes,
        )


def build_server():
    """Load launcher-owned local configuration and build a STDIO-only server."""
    policy_path = Path(
        os.environ.get("SCOUT_HOST_POLICY_FILE", ".local/authoring-policy.json")
    )
    principal = os.environ.get("SCOUT_LOCAL_PRINCIPAL", "")
    dsn = os.environ.get("SCOUT_DATABASE_URL", "")
    evidence_root = Path(
        os.environ.get(
            "SCOUT_EVIDENCE_ROOT",
            str(Path.home() / "Library/Application Support/Data Mozaic/scout-evidence"),
        )
    )
    if not principal or not dsn or not policy_path.is_file():
        raise RuntimeError(
            "set SCOUT_LOCAL_PRINCIPAL, SCOUT_DATABASE_URL, and an administrative policy file"
        )
    policy = LocalAuthorizationStore.from_file(policy_path)
    repo_root = Path(__file__).resolve().parents[4]
    if not evidence_root.is_absolute() or evidence_root.resolve().is_relative_to(
        repo_root
    ):
        raise RuntimeError(
            "SCOUT_EVIDENCE_ROOT must be absolute and outside the checkout"
        )
    custody = LocalFileEvidenceCustody(evidence_root)
    app = ScoutApplication(
        _LocalSourceLookup(policy),
        _LocalPolicyDecision(policy),
        PostgresScoutSessionRepository(dsn),
        custody,
        FixtureExplorer(),
    )
    return create_server(app, principal)
