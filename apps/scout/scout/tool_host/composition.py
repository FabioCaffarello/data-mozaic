"""Local development composition; never a production BC01/BC06/BC11 adapter."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from ..adapters.fixtures import FixtureExplorer
from ..adapters.local_custody import LocalFileEvidenceCustody
from ..adapters.postgres.repository import (
    PostgresNavigationBudgetLedger,
    PostgresScoutSessionRepository,
)
from ..application import ScoutApplication
from ..domain import AuthorizationError, PolicyDecisionRef, ScoutScope, SourceRef
from ..tools.discovery import HTTPDiscoveryTool
from ..tools.policy import LocalAuthorizationStore, ReloadingAuthorizationStore
from ..tools.ports import TrustedToolContext
from ..tools.transport import AiohttpSquidTransport
from .server import create_server

_LOG = logging.getLogger(__name__)


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
    artifact_root = Path(
        os.environ.get(
            "SCOUT_ARTIFACT_ROOT",
            str(Path.home() / ".local/share/data-mozaic/scout-artifacts"),
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
    if not artifact_root.is_absolute() or artifact_root.resolve().is_relative_to(
        repo_root
    ):
        raise RuntimeError(
            "SCOUT_ARTIFACT_ROOT must be absolute and outside the checkout"
        )
    custody = LocalFileEvidenceCustody(evidence_root)
    repository = PostgresScoutSessionRepository(dsn)

    def http_discovery_factory(caller: str, session_id: str, call_id: str):
        class Context:
            def current(self):
                return TrustedToolContext(caller, session_id, call_id)

        class Clock:
            def now(self):
                return datetime.now().astimezone()

        class NoEvidence:
            def store(self, *args, **kwargs):
                raise RuntimeError("HTTP evidence is persisted by BC14 after fetch")

        class Trace:
            def append(self, trace_session_id, event_type, fields, at):
                if trace_session_id != session_id:
                    raise AuthorizationError("trace session identity changed")
                _LOG.info(
                    "scout.http.trace event=%s fields=%s",
                    event_type,
                    fields,
                    extra={"scout_session_id": session_id},
                )

        return HTTPDiscoveryTool(
            Context(),
            ReloadingAuthorizationStore(policy_path),
            PostgresNavigationBudgetLedger(dsn),
            NoEvidence(),
            Trace(),
            Clock(),
            AiohttpSquidTransport(
                os.environ.get("SCOUT_EGRESS_PROXY", "http://127.0.0.1:3128")
            ),
            preserve_evidence=False,
        )

    app = ScoutApplication(
        _LocalSourceLookup(policy),
        _LocalPolicyDecision(policy),
        repository,
        custody,
        FixtureExplorer(),
        http_discovery_factory,
    )
    return create_server(app, principal, artifact_root)
