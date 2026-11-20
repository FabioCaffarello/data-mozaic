"""Local development composition; never a production BC01/BC06/BC11 adapter."""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
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
from .protected_replay import DockerProtectedReplayExecutor
from .server import create_server

_LOG = logging.getLogger(__name__)


def _private_policy_snapshot(source: Path, destination: Path) -> Path:
    if source.is_symlink() or not source.is_file():
        raise RuntimeError("local Scout policy file is unavailable")
    source_mode = source.stat()
    if source_mode.st_uid != os.geteuid() or source_mode.st_mode & 0o077:
        raise RuntimeError("local Scout policy file must be owner-only")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.parent.chmod(0o700)
    parent_mode = destination.parent.stat()
    if (
        not stat.S_ISDIR(parent_mode.st_mode)
        or parent_mode.st_uid != os.geteuid()
        or parent_mode.st_mode & 0o077
    ):
        raise RuntimeError("private Scout broker state directory is not protected")
    data = source.read_bytes()
    descriptor, temporary = tempfile.mkstemp(
        prefix=".policy-snapshot-", dir=destination.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        destination.chmod(0o400)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


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


def build_server(*, trusted_http: bool = False):
    """Compose the existing Scout application with local or operator-owned config."""
    trusted_http = trusted_http or os.environ.get("SCOUT_TRUSTED_HTTP") == "1"
    policy_path = Path(
        os.environ.get(
            "SCOUT_HOST_POLICY_FILE",
            "/etc/data-mozaic/scout/authoring-policy.json"
            if trusted_http
            else ".local/authoring-policy.json",
        )
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
    repo_root = None if trusted_http else Path(__file__).resolve().parents[4]
    if not policy_path.is_absolute() and not trusted_http:
        policy_path = repo_root / policy_path
    if not principal or not dsn or not policy_path.is_file():
        raise RuntimeError(
            "set SCOUT_LOCAL_PRINCIPAL, SCOUT_DATABASE_URL, and an administrative policy file"
        )
    if trusted_http:
        if not policy_path.is_absolute() or policy_path.is_symlink():
            raise RuntimeError(
                "trusted broker policy must use a protected absolute path"
            )
        private_state_root = Path(
            os.environ.get("SCOUT_RUNTIME_STATE_DIR", "/var/lib/data-mozaic/scout")
        )
        if not private_state_root.is_absolute():
            raise RuntimeError("trusted broker state directory must be absolute")
        private_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = private_state_root.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise RuntimeError(
                "trusted broker state must be private to its service identity"
            )
        policy_metadata = policy_path.stat()
        if policy_metadata.st_uid != os.geteuid() or policy_metadata.st_mode & 0o077:
            raise RuntimeError(
                "trusted broker policy must be service-owned and private"
            )
        if not os.environ.get("SCOUT_EVIDENCE_ROOT") or not os.environ.get(
            "SCOUT_ARTIFACT_ROOT"
        ):
            raise RuntimeError(
                "trusted broker evidence and artifact roots must be configured"
            )
        trusted_policy_path = policy_path
    else:
        private_state_root = Path.home() / ".local/state/data-mozaic/scout-bc15"
        trusted_policy_path = _private_policy_snapshot(
            policy_path, private_state_root / "policy-snapshot.json"
        )
    policy = LocalAuthorizationStore.from_file(trusted_policy_path)
    if not evidence_root.is_absolute() or (
        repo_root is not None and evidence_root.resolve().is_relative_to(repo_root)
    ):
        raise RuntimeError(
            "SCOUT_EVIDENCE_ROOT must be absolute and outside the checkout"
        )
    if not artifact_root.is_absolute() or (
        repo_root is not None and artifact_root.resolve().is_relative_to(repo_root)
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
            ReloadingAuthorizationStore(trusted_policy_path),
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
    acceptance_expectations = {}
    expectation_files = []
    if trusted_http:
        configured_expectations = os.environ.get(
            "SCOUT_ACCEPTANCE_EXPECTATIONS_FILE", ""
        )
        if configured_expectations:
            expectation_file = Path(configured_expectations)
            if not expectation_file.is_absolute() or expectation_file.is_symlink():
                raise RuntimeError("trusted acceptance oracle path is invalid")
            metadata = expectation_file.stat()
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
                raise RuntimeError(
                    "trusted acceptance oracle must be service-owned and private"
                )
            expectation_files = [expectation_file]
    else:
        expectation_root = repo_root / "tests/fixtures/scout-authoring"
        expectation_files = [
            expectation_root / filename
            for filename in (
                "controlled-catalog-acceptance.json",
                "controlled-catalog-codex-acceptance.json",
                "single-resource-acceptance.json",
            )
        ]
    for expectation_file in expectation_files:
        expectation = json.loads(expectation_file.read_text(encoding="utf-8"))
        source = expectation.get("source", {})
        source_key = (source.get("source_id"), source.get("revision"))
        if not all(source_key) or source_key in acceptance_expectations:
            raise RuntimeError("trusted BC15 acceptance oracle registry is invalid")
        acceptance_expectations[source_key] = expectation
    worker_image = os.environ.get(
        "SCOUT_BC15_WORKER_IMAGE_DIGEST"
        if trusted_http
        else "SCOUT_TEST_WORKER_IMAGE_DIGEST",
        "",
    )
    if not worker_image:
        raise RuntimeError("a prebuilt BC15 worker image digest is required")
    executor = DockerProtectedReplayExecutor(
        worker_image,
        registry_path=private_state_root / "bc15-runs.sqlite3",
        docker_context=os.environ.get("SCOUT_DOCKER_CONTEXT", "default"),
        docker_endpoint=os.environ.get(
            "SCOUT_DOCKER_ENDPOINT", "unix:///var/run/docker.sock"
        ),
    )
    return create_server(
        app,
        principal,
        artifact_root,
        protected_replay_executor=executor,
        acceptance_expectations=acceptance_expectations,
        single_use_approval_source=("e2e-catalog-single", "single-resource-v1"),
    )


def build_trusted_http_server():
    """Construct the operator-configured HTTP service; never use checkout defaults."""
    if os.environ.get("SCOUT_TRUSTED_HTTP") != "1":
        raise RuntimeError("trusted HTTP mode is not enabled by the service manager")
    return build_server(trusted_http=True)
