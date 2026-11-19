"""Offline application workflow; validation of public JSON belongs at the edge."""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from mozaic_observability import bind_context, log_event

from .domain import (
    AuthorizationError,
    ConflictError,
    DiscoveryFinding,
    EvidenceManifest,
    EvidenceUnavailableError,
    IntegrityError,
    PlaybookProposal,
    ScoutObjective,
    ScoutScope,
    ScoutSession,
    SourceObservation,
    SourceRef,
    SourceRevisionConflict,
    State,
)
from .ports import (
    CUSTODY_STATUS_AVAILABLE,
    EvidenceCustodyPort,
    FixtureExplorationPort,
    HTTPDiscoveryFactoryPort,
    PolicyDecisionPort,
    ScoutSessionRepositoryPort,
    SourceLookupPort,
)
from .projections import projection

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class HTTPExplorationOutcome:
    session: ScoutSession
    text: str
    truncated: bool


class ScoutApplication:
    def __init__(
        self,
        sources: SourceLookupPort,
        policy: PolicyDecisionPort,
        repository: ScoutSessionRepositoryPort,
        custody: EvidenceCustodyPort,
        fixture_explorer: FixtureExplorationPort | None = None,
        http_discovery_factory: HTTPDiscoveryFactoryPort | None = None,
    ):
        self.sources, self.policy, self.repository = sources, policy, repository
        self.custody = custody
        self.fixture_explorer = fixture_explorer
        self.http_discovery_factory = http_discovery_factory

    @staticmethod
    def _trace(event: str, session_id: str, command_id: str, state: str) -> None:
        with bind_context(session_id=session_id, command_id=command_id):
            log_event(
                _LOGGER,
                logging.INFO,
                event,
                component="BC14",
                state=state,
                outcome="completed",
            )

    def start_incremental(
        self,
        *,
        source: SourceRef,
        objective: str,
        principal: str,
        command_id: str,
        now: datetime,
    ) -> ScoutSession:
        """Start an authorized session without running the fixture demonstration.

        `command_id` is supplied by trusted host composition and is also the
        idempotency key/session ID for this first command. Identity, scope, and
        budgets are deliberately absent from this API's model-controlled input.
        """
        if not principal or not command_id or len(command_id) > 128:
            raise IntegrityError("trusted command identity is required")
        source_scope = self.sources.resolve(source)
        decision = self.policy.decide(principal, source, now)
        digest = hashlib.sha256(
            json.dumps(
                {"source": asdict(source), "objective": objective},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        header = self.repository.find_intent_header(principal, command_id, digest)
        if header is not None:
            if header["source"] != source:
                raise IntegrityError("command replay changed source revision")
            prior = self.repository.get_command_result(principal, command_id, digest)
            if prior is None:
                prior = self.repository.find_intent(principal, command_id, digest)
            if prior is None or prior.principal != principal:
                raise IntegrityError("authorized command receipt has no session")
            prior._policy(decision, now)
            self._trace(
                "scout.incremental.start_replayed",
                prior.session_id,
                command_id,
                prior.state.value,
            )
            return prior
        requested_scope = source_scope
        session = ScoutSession(
            command_id,
            source,
            ScoutObjective(objective),
            requested_scope,
            principal,
        )
        session.authorize(source_scope, decision, now)
        session.start(decision, now)
        try:
            self.repository.create(
                session,
                command_id,
                digest,
                command_kind="StartScout",
                receipt_status="completed",
            )
        except ConflictError:
            # Resolve a concurrent identical start from its durable receipt.
            header = self.repository.find_intent_header(principal, command_id, digest)
            replay = (
                self.repository.get_command_result(principal, command_id, digest)
                if header is not None
                else None
            )
            if replay is None or replay.source != source:
                raise
            replay._policy(decision, now)
            self._trace(
                "scout.incremental.start_replayed",
                replay.session_id,
                command_id,
                replay.state.value,
            )
            return replay
        self._trace(
            "scout.incremental.started",
            session.session_id,
            command_id,
            session.state.value,
        )
        return session

    def _begin_incremental_command(
        self,
        *,
        session: ScoutSession,
        principal: str,
        command_id: str,
        idempotency_key: str,
        fingerprint: str,
        command_kind: str,
        intent: dict,
    ) -> ScoutSession | None:
        header = self.repository.find_command_header(
            principal, idempotency_key, fingerprint
        )
        if header is not None:
            if header["source"] != session.source:
                raise IntegrityError("command receipt source mismatch")
            if header["status"] == "completed":
                result = self.repository.get_command_result(
                    principal, idempotency_key, fingerprint
                )
                if result is None:
                    raise IntegrityError("completed command has no result snapshot")
                return result
        self.repository.begin_command(
            principal,
            session.session_id,
            idempotency_key,
            command_id,
            command_kind,
            fingerprint,
            intent,
        )
        return None

    def _complete_incremental_command(
        self,
        *,
        session: ScoutSession,
        expected_version: int,
        principal: str,
        idempotency_key: str,
        fingerprint: str,
    ) -> ScoutSession:
        try:
            self.repository.complete_command(
                session,
                expected_version,
                principal,
                idempotency_key,
                fingerprint,
            )
            return session
        except ConflictError:
            # A concurrent identical invocation may have committed after our
            # initial receipt read. Return that durable result, never recount it.
            result = self.repository.get_command_result(
                principal, idempotency_key, fingerprint
            )
            if result is None:
                self.repository.abandon_command(principal, idempotency_key)
                raise
            return result

    def explore_fixture_incremental(
        self,
        *,
        session_id: str,
        principal: str,
        command_id: str,
        idempotency_key: str,
        fingerprint: str,
        expected_version: int,
        fixture_id: str,
        now: datetime,
    ) -> ScoutSession:
        """Explore a server-registered fixture, verify custody, then seal evidence."""
        if self.fixture_explorer is None:
            raise IntegrityError("fixture exploration is not configured")
        session, decision = self.authorized_session(session_id, principal, now)
        header = self.repository.find_command_header(
            principal, idempotency_key, fingerprint
        )
        if header is not None and header["status"] == "completed":
            replay = self.repository.get_command_result(
                principal, idempotency_key, fingerprint
            )
            if replay is None:
                raise IntegrityError("completed exploration has no result snapshot")
            return replay
        if session.remaining_requests < 1 or session.remaining_seconds < 1:
            raise IntegrityError("authorized session budget is exhausted")
        if session.version != expected_version:
            raise ConflictError("optimistic concurrency conflict")
        page = self.fixture_explorer.explore(
            session.source,
            session.authorization.scope,
            fixture_id,
            decision.max_response_bytes,
        )
        if (
            page.fixture_id != fixture_id
            or page.status != 200
            or page.content_type not in {"text/html", "text/plain"}
            or not isinstance(page.body, bytes)
            or len(page.body) > min(decision.max_response_bytes, 2_000_000)
            or not session.authorization.scope.allows(page.origin, page.path)
            or not 0 <= page.elapsed_seconds <= session.remaining_seconds
            or any(
                not session.authorization.scope.allows(link.origin, link.path)
                for link in page.links
            )
        ):
            raise IntegrityError("fixture result violates approved scope or limits")
        if any(observation.url == page.url for observation in session.observations):
            raise IntegrityError("session already contains an observation for this URL")
        intent = {
            "session_id": session_id,
            "source": asdict(session.source),
            "fixture_id": fixture_id,
            "expected_version": expected_version,
        }
        replay = self._begin_incremental_command(
            session=session,
            principal=principal,
            command_id=command_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            command_kind="ExploreFixture",
            intent=intent,
        )
        if replay is not None:
            return replay
        artifact_id = (
            "artifact-"
            + hashlib.sha256(f"{session_id}\0{command_id}".encode()).hexdigest()
        )
        digest = hashlib.sha256(page.body).hexdigest()
        self.repository.begin_custody_intent(
            artifact_id, session_id, digest, len(page.body)
        )
        try:
            artifact = self.custody.lookup(artifact_id)
            if artifact is None:
                artifact = self.custody.store(
                    session_id, session.source, page.body, artifact_id
                )
        except Exception:
            self.repository.mark_custody_unknown(artifact_id)
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise
        if (
            artifact.artifact_id != artifact_id
            or artifact.session_id != session_id
            or artifact.source != session.source
            or artifact.sha256 != digest
            or artifact.size_bytes != len(page.body)
            or artifact.kind != "http_response"
            or artifact.custody_status != CUSTODY_STATUS_AVAILABLE
            or not self.custody.verify(artifact)
        ):
            self.repository.mark_custody_discrepancy(
                artifact_id, "fixture_evidence_integrity_mismatch"
            )
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise IntegrityError(
                "custody receipt failed digest or identity verification"
            )
        self.repository.confirm_custody_intent(artifact_id, digest, asdict(artifact))
        observation_id = (
            "observation-"
            + hashlib.sha256(f"{session_id}\0{command_id}".encode()).hexdigest()
        )
        observation = SourceObservation(
            observation_id,
            session_id,
            session.source,
            f"Fixture response captured: HTTP {page.status} {page.content_type}.",
            artifact_id,
            page.url,
            page.status,
            page.content_type,
            page.title,
            tuple(link.url for link in page.links),
        )
        session.observe(observation, decision, now, page.elapsed_seconds)
        manifest_id = (
            "manifest-"
            + hashlib.sha256(f"{session_id}\0{artifact_id}".encode()).hexdigest()
        )
        manifest = EvidenceManifest(
            manifest_id,
            session_id,
            session.source,
            now.isoformat().replace("+00:00", "Z"),
            (artifact,),
        )
        session.seal(manifest, self.custody.verify(artifact), decision, now)
        session = self._complete_incremental_command(
            session=session,
            expected_version=expected_version,
            principal=principal,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
        self.repository.confirm_custody_intent(artifact_id, digest, asdict(artifact))
        self._trace(
            "scout.fixture.explored",
            session.session_id,
            command_id,
            session.state.value,
        )
        return session

    async def explore_http_incremental(
        self,
        *,
        session_id: str,
        principal: str,
        command_id: str,
        idempotency_key: str,
        fingerprint: str,
        expected_version: int,
        url: str,
        now: datetime,
    ) -> HTTPExplorationOutcome:
        """Fetch one authorized URL and attach the original bytes to BC14 evidence."""
        if self.http_discovery_factory is None:
            raise IntegrityError("authorized HTTP discovery is not configured")
        session, decision = self.authorized_session(session_id, principal, now)
        header = self.repository.find_command_header(
            principal, idempotency_key, fingerprint
        )
        discovery = self.http_discovery_factory(principal, session_id, command_id)
        if header is not None and header["status"] == "completed":
            if header["source"] != session.source:
                raise IntegrityError("HTTP replay source revision mismatch")
            replay = self.repository.get_command_result(
                principal, idempotency_key, fingerprint
            )
            if replay is None:
                raise IntegrityError("completed HTTP command has no result snapshot")
            observation_id = (
                "observation-"
                + hashlib.sha256(
                    f"{session_id}\0{header['command_id']}".encode()
                ).hexdigest()
            )
            observation = next(
                (
                    item
                    for item in replay.observations
                    if item.observation_id == observation_id
                ),
                None,
            )
            try:
                replay_target = (
                    discovery.normalize_target(observation.url)
                    if observation is not None and observation.url is not None
                    else None
                )
            except Exception as error:
                raise EvidenceUnavailableError(
                    "authorized HTTP replay target is invalid"
                ) from error
            if (
                replay.session_id != session_id
                or replay.principal != principal
                or replay.source != session.source
                or observation is None
                or observation.source != session.source
                or observation.session_id != session_id
                or observation.url is None
                or observation.content_type not in {"text/html", "text/plain"}
                or not observation.extraction_version
                or replay_target is None
                or not session.authorization.scope.allows(
                    replay_target.origin, replay_target.path
                )
            ):
                raise EvidenceUnavailableError(
                    "authorized HTTP replay metadata is unavailable"
                )
            manifests = [
                item
                for item in replay.manifests
                if any(
                    artifact.artifact_id == observation.artifact_id
                    for artifact in item.artifacts
                )
            ]
            if len(manifests) != 1 or len(manifests[0].artifacts) != 1:
                raise EvidenceUnavailableError(
                    "authorized HTTP replay manifest is unavailable"
                )
            artifact = manifests[0].artifacts[0]
            if (
                manifests[0].session_id != session_id
                or manifests[0].source != session.source
                or artifact.artifact_id != observation.artifact_id
                or artifact.session_id != session_id
                or artifact.source != session.source
                or artifact.kind != "http_response"
                or artifact.custody_status != CUSTODY_STATUS_AVAILABLE
                or artifact.size_bytes is None
                or artifact.size_bytes > min(decision.max_response_bytes, 2_000_000)
            ):
                raise EvidenceUnavailableError(
                    "authorized HTTP replay artifact is unavailable"
                )
            try:
                body = self.custody.read(artifact)
            except Exception as error:
                raise EvidenceUnavailableError(
                    "authorized HTTP replay custody read failed"
                ) from error
            if (
                body is None
                or not isinstance(body, bytes)
                or len(body) != artifact.size_bytes
                or hashlib.sha256(body).hexdigest() != artifact.sha256
                or len(body) > min(decision.max_response_bytes, 2_000_000)
            ):
                raise EvidenceUnavailableError(
                    "authorized HTTP replay bytes are unavailable or invalid"
                )
            try:
                text, truncated = discovery.reconstruct_text(
                    body, observation.content_type, observation.extraction_version
                )
            except Exception as error:
                raise EvidenceUnavailableError(
                    "authorized HTTP replay projection is unavailable"
                ) from error
            if not isinstance(text, str) or len(text.encode("utf-8")) > 64_000:
                raise EvidenceUnavailableError(
                    "authorized HTTP replay projection exceeds its limit"
                )
            return HTTPExplorationOutcome(replay, text, truncated)
        if session.version != expected_version:
            raise ConflictError("optimistic concurrency conflict")
        if session.remaining_requests < 1 or session.remaining_seconds < 1:
            raise IntegrityError("authorized session budget is exhausted")
        normalized = discovery.normalize_target(url)
        if not session.authorization.scope.allows(normalized.origin, normalized.path):
            raise AuthorizationError("target is outside the approved source scope")
        if any(
            observation.url == normalized.url for observation in session.observations
        ):
            raise IntegrityError("session already contains an observation for this URL")
        intent = {
            "session_id": session_id,
            "source": asdict(session.source),
            "url_sha256": hashlib.sha256(normalized.url.encode()).hexdigest(),
            "expected_version": expected_version,
        }
        replay = self._begin_incremental_command(
            session=session,
            principal=principal,
            command_id=command_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            command_kind="ExploreHTTP",
            intent=intent,
        )
        if replay is not None:
            return replay
        try:
            result = await discovery.fetch(
                session.source.source_id, session.source.revision, normalized.url
            )
        except BaseException:
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise
        if (
            result.status < 100
            or result.status > 599
            or result.content_type not in {"text/html", "text/plain"}
            or not isinstance(result.body, bytes)
            or not isinstance(result.extraction_version, str)
            or not result.extraction_version
            or not isinstance(result.completed_at, datetime)
            or result.completed_at.tzinfo is None
            or len(result.body) > min(decision.max_response_bytes, 2_000_000)
            or not 1 <= result.request_count <= session.remaining_requests
            or not 0 <= result.elapsed_seconds <= session.remaining_seconds
        ):
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise IntegrityError(
                "HTTP result violates approved response or budget limits"
            )
        try:
            current_decision = self.policy.decide(
                principal, session.source, result.completed_at
            )
            session._policy(current_decision, result.completed_at)
            if len(result.body) > current_decision.max_response_bytes:
                raise AuthorizationError(
                    "response exceeds the current approved byte limit"
                )
            current_source_scope = self.sources.resolve(session.source)
            if (
                session.authorization.scope.intersect(current_source_scope)
                != session.authorization.scope
            ):
                raise SourceRevisionConflict(
                    "source revision changed during HTTP fetch"
                )
        except Exception:
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise
        final_target = discovery.normalize_target(result.final_url)
        if not session.authorization.scope.allows(
            final_target.origin, final_target.path
        ):
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise AuthorizationError("final redirect target is outside approved scope")
        try:
            for link in result.links:
                normalized_link = discovery.normalize_target(link)
                if not session.authorization.scope.allows(
                    normalized_link.origin, normalized_link.path
                ):
                    raise AuthorizationError(
                        "discovered link is outside approved scope"
                    )
        except Exception:
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise
        artifact_id = (
            "artifact-"
            + hashlib.sha256(f"{session_id}\0{command_id}".encode()).hexdigest()
        )
        digest = hashlib.sha256(result.body).hexdigest()
        self.repository.begin_custody_intent(
            artifact_id, session_id, digest, len(result.body)
        )
        try:
            artifact = self.custody.lookup(artifact_id)
            if artifact is None:
                artifact = self.custody.store(
                    session_id, session.source, result.body, artifact_id
                )
        except BaseException:
            self.repository.mark_custody_unknown(artifact_id)
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise
        if (
            artifact.artifact_id != artifact_id
            or artifact.session_id != session_id
            or artifact.source != session.source
            or artifact.sha256 != digest
            or artifact.size_bytes != len(result.body)
            or artifact.kind != "http_response"
            or artifact.custody_status != CUSTODY_STATUS_AVAILABLE
            or not self.custody.verify(artifact)
        ):
            self.repository.mark_custody_discrepancy(
                artifact_id, "http_evidence_integrity_mismatch"
            )
            self.repository.mark_command_uncertain(principal, idempotency_key)
            raise IntegrityError("HTTP evidence receipt failed integrity verification")
        self.repository.confirm_custody_intent(artifact_id, digest, asdict(artifact))
        observation_id = (
            "observation-"
            + hashlib.sha256(f"{session_id}\0{command_id}".encode()).hexdigest()
        )
        observation = SourceObservation(
            observation_id,
            session_id,
            session.source,
            f"HTTP response captured: {result.status} {result.content_type}.",
            artifact_id,
            final_target.url,
            result.status,
            result.content_type,
            result.title,
            tuple(result.links),
            result.extraction_version,
        )
        session.observe(
            observation,
            current_decision,
            result.completed_at,
            result.elapsed_seconds,
            result.request_count,
        )
        manifest_id = (
            "manifest-"
            + hashlib.sha256(f"{session_id}\0{artifact_id}".encode()).hexdigest()
        )
        session.seal(
            EvidenceManifest(
                manifest_id,
                session_id,
                session.source,
                result.completed_at.isoformat().replace("+00:00", "Z"),
                (artifact,),
            ),
            self.custody.verify(artifact),
            current_decision,
            result.completed_at,
        )
        session = self._complete_incremental_command(
            session=session,
            expected_version=expected_version,
            principal=principal,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
        self.repository.confirm_custody_intent(artifact_id, digest, asdict(artifact))
        self._trace(
            "scout.http.explored", session.session_id, command_id, session.state.value
        )
        return HTTPExplorationOutcome(session, result.text, result.truncated)

    def authorized_session(
        self, session_id: str, principal: str, now: datetime
    ) -> tuple[ScoutSession, object]:
        """Reauthorize using metadata before loading the stored aggregate snapshot."""
        header = self.repository.get_session_header(session_id)
        if header is None:
            raise IntegrityError("session not found")
        if header["principal"] != principal:
            raise IntegrityError("session principal mismatch")
        self.sources.resolve(header["source"])
        decision = self.policy.decide(principal, header["source"], now)
        session = self.repository.get_session(session_id)
        if session is None or session.source != header["source"]:
            raise IntegrityError("session changed while authorizing snapshot")
        session._policy(decision, now)
        source_scope = self.sources.resolve(session.source)
        if (
            session.authorization.scope.intersect(source_scope)
            != session.authorization.scope
        ):
            raise SourceRevisionConflict(
                "source revision no longer supports session scope"
            )
        return session, decision

    def record_finding_incremental(
        self,
        *,
        session_id: str,
        principal: str,
        command_id: str,
        idempotency_key: str,
        fingerprint: str,
        expected_version: int,
        statement: str,
        epistemic_status: str,
        confidence: float,
        manifest_ids: tuple[str, ...],
        verification_note: str,
        now: datetime,
    ) -> ScoutSession:
        """Append an evidence-backed finding using aggregate checks and CAS."""
        session, decision = self.authorized_session(session_id, principal, now)
        header = self.repository.find_command_header(
            principal, idempotency_key, fingerprint
        )
        if header is not None and header["status"] == "completed":
            replay = self.repository.get_command_result(
                principal, idempotency_key, fingerprint
            )
            if replay is None:
                raise IntegrityError("completed finding command has no result snapshot")
            return replay
        if session.version != expected_version or not command_id:
            raise ConflictError("expected version or command identity is invalid")
        finding = DiscoveryFinding(
            "finding-" + command_id,
            session.session_id,
            session.source,
            statement,
            epistemic_status,
            confidence,
            manifest_ids,
            verification_note,
        )
        # Validate domain rules before creating a durable in-progress receipt.
        session.add_finding(finding, decision, now)
        replay = self._begin_incremental_command(
            session=session,
            principal=principal,
            command_id=command_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            command_kind="RecordFinding",
            intent={"session_id": session_id, "expected_version": expected_version},
        )
        if replay is not None:
            return replay
        committed = self._complete_incremental_command(
            session=session,
            expected_version=expected_version,
            principal=principal,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
        self._trace(
            "scout.finding.recorded",
            committed.session_id,
            command_id,
            committed.state.value,
        )
        return committed

    def submit_proposal_incremental(
        self,
        *,
        session_id: str,
        principal: str,
        expected_version: int,
        command_id: str,
        idempotency_key: str,
        fingerprint: str,
        summary: str,
        finding_ids: tuple[str, ...],
        manifest_ids: tuple[str, ...],
        assumptions: tuple[str, ...],
        risks: tuple[str, ...],
        required_capabilities: tuple[str, ...],
        now: datetime,
    ) -> ScoutSession:
        """Persist a BC14 proposal; repository outbox owns BC15 delivery intent."""
        session, decision = self.authorized_session(session_id, principal, now)
        header = self.repository.find_command_header(
            principal, idempotency_key, fingerprint
        )
        if header is not None and header["status"] == "completed":
            replay = self.repository.get_command_result(
                principal, idempotency_key, fingerprint
            )
            if replay is None:
                raise IntegrityError("completed proposal has no result snapshot")
            return replay
        if session.version != expected_version or not command_id:
            raise ConflictError("expected version or command identity is invalid")
        proposal = PlaybookProposal(
            "proposal-" + command_id,
            session.session_id,
            session.source,
            summary,
            finding_ids,
            manifest_ids,
            assumptions,
            risks,
            required_capabilities,
        )
        # Validate proposal references before persisting its command intent.
        session.propose(proposal, decision, now)
        session.handoff_result = "delivery_pending"
        replay = self._begin_incremental_command(
            session=session,
            principal=principal,
            command_id=command_id,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
            command_kind="SubmitProposal",
            intent={"session_id": session_id, "expected_version": expected_version},
        )
        if replay is not None:
            return replay
        committed = self._complete_incremental_command(
            session=session,
            expected_version=expected_version,
            principal=principal,
            idempotency_key=idempotency_key,
            fingerprint=fingerprint,
        )
        self._trace(
            "scout.proposal.submitted",
            committed.session_id,
            command_id,
            committed.state.value,
        )
        return committed

    def status_incremental(
        self, session_id: str, principal: str, now: datetime
    ) -> dict:
        """Return an authorized, redacted projection without evidence payloads."""
        session, _ = self.authorized_session(session_id, principal, now)
        return {
            "session_id": session.session_id,
            "source": asdict(session.source),
            "state": session.state.value,
            "version": session.version,
            "remaining_requests": session.remaining_requests,
            "remaining_seconds": session.remaining_seconds,
            "observation_ids": [item.observation_id for item in session.observations],
            "manifest_ids": [item.manifest_id for item in session.manifests],
            "finding_ids": [item.finding_id for item in session.findings],
            "proposal_id": session.proposal.proposal_id if session.proposal else None,
            "handoff_result": session.handoff_result,
        }

    def export_transcript(self, session_id: str, principal: str, now: datetime) -> dict:
        """Build a redacted BC14→BC15 DTO from reauthorized verified state."""
        session, decision = self.authorized_session(session_id, principal, now)
        if session.proposal is None or not session.findings or not session.manifests:
            raise IntegrityError("session lacks evidence-backed proposal state")
        if self.repository.has_incomplete_commands(session_id):
            raise IntegrityError("session has an in-progress or uncertain command")
        if len(decision.scope.origins) != 1:
            raise IntegrityError("transcript v1 requires one approved origin")
        evidence_ids = {manifest.manifest_id for manifest in session.manifests}
        finding_ids = {finding.finding_id for finding in session.findings}
        if (
            session.proposal.source != session.source
            or session.proposal.session_id != session.session_id
            or not set(session.proposal.finding_ids) <= finding_ids
            or not set(session.proposal.manifest_ids) <= evidence_ids
        ):
            raise IntegrityError("proposal references do not match session snapshot")
        observation_artifacts = {item.artifact_id for item in session.observations}
        proposal_manifest_ids = set(session.proposal.manifest_ids)
        proposal_finding_ids = set(session.proposal.finding_ids)
        pages = []
        manifests_by_artifact = {}
        artifact_metadata = {}
        artifact_refs = []
        for manifest in session.manifests:
            if (
                manifest.session_id != session.session_id
                or manifest.source != session.source
            ):
                raise IntegrityError("manifest source/session mismatch")
            if manifest.manifest_id not in proposal_manifest_ids:
                continue
            for artifact in manifest.artifacts:
                if (
                    artifact.artifact_id not in observation_artifacts
                    or artifact.session_id != session.session_id
                    or artifact.source != session.source
                    or not self.custody.verify(artifact)
                ):
                    raise IntegrityError("transcript evidence verification failed")
                manifests_by_artifact[artifact.artifact_id] = manifest.manifest_id
                artifact_metadata[artifact.artifact_id] = artifact
                artifact_refs.append(
                    {
                        "artifact_id": artifact.artifact_id,
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "kind": artifact.kind,
                        "redaction": artifact.redaction,
                        "manifest_id": manifest.manifest_id,
                    }
                )
        for observation in sorted(
            session.observations, key=lambda item: item.observation_id
        ):
            if observation.artifact_id not in artifact_metadata:
                continue
            manifest_id = manifests_by_artifact.get(observation.artifact_id)
            artifact = artifact_metadata.get(observation.artifact_id)
            if not manifest_id or not observation.url or artifact is None:
                raise IntegrityError(
                    "observation lacks a transcript-ready manifest or URL"
                )
            pages.append(
                {
                    "url": observation.url,
                    "evidence_ref": manifest_id,
                    "artifact_id": artifact.artifact_id,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "discovered_links": sorted(set(observation.links)),
                }
            )
        findings = [
            {
                "finding_id": finding.finding_id,
                "classification": {
                    "observation": "observed",
                    "hypothesis": "hypothesized",
                }.get(finding.epistemic_status, "unsupported"),
                "evidence_refs": sorted(finding.manifest_ids),
            }
            for finding in sorted(session.findings, key=lambda item: item.finding_id)
            if finding.finding_id in proposal_finding_ids
        ]
        if any(item["classification"] == "unsupported" for item in findings):
            raise IntegrityError("unsupported epistemic status in transcript")
        origin = decision.scope.origins[0]
        prefixes = sorted(decision.scope.path_prefixes)
        budgets = {
            "max_requests": decision.scope.max_requests,
            "max_pages": min(decision.scope.max_requests, 20),
            "max_response_bytes": decision.max_response_bytes,
        }
        approval = {
            "approval_id": decision.decision_id,
            "source_id": session.source.source_id,
            "revision": session.source.revision,
            "origin": origin,
            "path_prefixes": prefixes,
            "budgets": budgets,
        }
        transcript = {
            "transcript_schema": "scout-transcript-draft/v1",
            "producer": {
                "bounded_context": "BC14",
                "tool_host": "data-mozaic-scout-mcp/1",
            },
            "session": {
                "session_id": session.session_id,
                "version": session.version,
                "state": session.state.value,
                "source": asdict(session.source),
                "objective": session.objective.text,
            },
            "approval_record": approval,
            "approved_scope": {
                "source_id": session.source.source_id,
                "revision": session.source.revision,
                "origin": origin,
                "path_prefixes": prefixes,
                "budgets": budgets,
            },
            "objective": session.objective.text,
            "evidence": sorted(artifact_refs, key=lambda item: item["artifact_id"]),
            "proposal": projection(session.proposal),
            "findings": findings,
            "pages": pages,
            "redaction": {"raw_content_included": False, "policy": "metadata-only/v1"},
            "integrity": {
                "custody_verified": True,
                "approval_authentication": "local-reference-not-cryptographically-signed",
            },
        }
        self._trace(
            "scout.transcript.exported",
            session.session_id,
            "export",
            session.state.value,
        )
        return transcript

    def reconcile_custody_intent(self, artifact_id: str) -> str:
        """Resolve an uncertain BC06 write by its durable identity and digest."""
        intent = self.repository.get_custody_intent(artifact_id)
        if intent is None:
            return "unknown_intent"
        artifact = self.custody.lookup(artifact_id)
        if artifact is None:
            return "not_found"
        if (
            artifact.artifact_id != artifact_id
            or artifact.session_id != intent["session_id"]
            or artifact.source != intent["source"]
            or artifact.sha256 != intent["sha256"]
            or artifact.size_bytes is None
            or artifact.size_bytes < 0
            or (
                intent["expected_size_bytes"] is not None
                and artifact.size_bytes != intent["expected_size_bytes"]
            )
            or artifact.custody_status != CUSTODY_STATUS_AVAILABLE
            or not self.custody.verify(artifact)
        ):
            self.repository.mark_custody_discrepancy(
                artifact_id, "identity_or_digest_mismatch"
            )
            return "manual_review"
        self.repository.confirm_custody_intent(
            artifact_id, intent["sha256"], asdict(artifact)
        )
        return "confirmed"

    def record_bc06_orphan_candidate(
        self, review_id: str, artifact_id: str, diagnostic: dict
    ) -> None:
        """Record a BC06-reported candidate; Scout does not perform global inventory."""
        self.repository.record_orphan_review(review_id, artifact_id, diagnostic)

    def run(
        self, request: dict, principal: str, fixture: bytes, now: datetime
    ) -> ScoutSession:
        # Caller must validate request against ScoutRequest schema before invoking this method.
        if not principal or request["requested_by"] != principal:
            raise IntegrityError("authenticated principal does not match requester")
        with bind_context(command_id=request.get("request_id")):
            log_event(_LOGGER, logging.INFO, "scout.command.accepted")
        digest = hashlib.sha256(
            json.dumps(
                {
                    "request": request,
                    "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        key = request["idempotency_key"]
        header = self.repository.find_intent_header(principal, key, digest)
        if header is not None:
            if header["principal"] != principal:
                raise IntegrityError("receipt principal mismatch")
            # Resolve authorization from receipt metadata before loading its snapshot.
            self.sources.resolve(header["source"])
            self.policy.decide(principal, header["source"], now)
        session = (
            self.repository.find_intent(principal, key, digest) if header else None
        )
        if session:
            if session.principal != principal or session.source != header["source"]:
                raise IntegrityError(
                    "receipt identity or fixed source revision mismatch"
                )
            # A receipt replay is still an authorized read. Recheck BC01 and BC11
            # before returning a previously committed result.
            resolved_scope = self.sources.resolve(session.source)
            decision = self.policy.decide(principal, session.source, now)
            if session.state == State.COMPLETED:
                session._policy(decision, now)
                if (
                    session.authorization.scope.intersect(resolved_scope)
                    != session.authorization.scope
                ):
                    raise SourceRevisionConflict(
                        "source revision no longer supports authorized scope"
                    )
                with bind_context(
                    command_id=request.get("request_id"),
                    session_id=session.session_id,
                ):
                    log_event(
                        _LOGGER,
                        logging.INFO,
                        "scout.command.replayed",
                        state=session.state.value,
                    )
                return session
        if session is None:
            source = SourceRef(**request["source"])
            source_scope = self.sources.resolve(source)
            decision = self.policy.decide(principal, source, now)
            raw = request["requested_scope"]
            scope = ScoutScope(
                tuple(raw["origins"]),
                tuple(raw["path_prefixes"]),
                tuple(raw["methods"]),
                raw["max_requests"],
                raw["max_duration_seconds"],
            )
            session = ScoutSession(
                request["request_id"],
                source,
                ScoutObjective(request["objective"]),
                scope,
                principal,
            )
            session.authorize(source_scope, decision, now)
            session.start(decision, now)
            self.repository.create(session, key, digest)
        decision = self.policy.decide(principal, session.source, now)
        if not session.proposal:
            artifact_id = "artifact-" + session.session_id
            fixture_digest = hashlib.sha256(fixture).hexdigest()
            self.repository.begin_custody_intent(
                artifact_id, session.session_id, fixture_digest, len(fixture)
            )
            try:
                artifact = self.custody.lookup(artifact_id)
                if artifact is None:
                    artifact = self.custody.store(
                        session.session_id, session.source, fixture, artifact_id
                    )
            except Exception:
                self.repository.mark_custody_unknown(artifact_id)
                with bind_context(
                    command_id=request.get("request_id"),
                    session_id=session.session_id,
                    effect_id=artifact_id,
                ):
                    log_event(
                        _LOGGER,
                        logging.WARNING,
                        "scout.custody.outcome_unknown",
                        state="unknown",
                    )
                raise
            if (
                artifact.artifact_id != artifact_id
                or artifact.session_id != session.session_id
                or artifact.source != session.source
                or artifact.sha256 != fixture_digest
                or artifact.size_bytes != len(fixture)
                or artifact.custody_status != CUSTODY_STATUS_AVAILABLE
                or not self.custody.verify(artifact)
            ):
                self.repository.mark_custody_discrepancy(
                    artifact_id, "identity_size_digest_or_integrity_mismatch"
                )
                raise IntegrityError("custody result not verified against intent")
            self.repository.confirm_custody_intent(
                artifact_id, fixture_digest, asdict(artifact)
            )
            observation = SourceObservation(
                "observation-" + session.session_id,
                session.session_id,
                session.source,
                "Fixture local examinada.",
                artifact.artifact_id,
            )
            session.observe(observation, decision, now)
            manifest = EvidenceManifest(
                "manifest-" + session.session_id,
                session.session_id,
                session.source,
                now.isoformat().replace("+00:00", "Z"),
                (artifact,),
            )
            session.seal(
                manifest,
                all(self.custody.verify(a) for a in manifest.artifacts),
                decision,
                now,
            )
            finding = DiscoveryFinding(
                "finding-" + session.session_id,
                session.session_id,
                session.source,
                observation.statement,
                "observation",
                1.0,
                (manifest.manifest_id,),
                "Observação da fixture; não generalizada.",
            )
            session.add_finding(finding, decision, now)
            proposal = PlaybookProposal(
                "proposal-" + session.session_id,
                session.session_id,
                session.source,
                "Investigar a estrutura observada na fixture.",
                (finding.finding_id,),
                (manifest.manifest_id,),
                ("Estrutura pode mudar.",),
                ("Observação limitada à fixture.",),
                ("http-read",),
            )
            session.propose(proposal, decision, now)
            session.handoff_result = "delivery_pending"
            self.repository.save(session, session.version)
        decision = self.policy.decide(principal, session.source, now)
        session._policy(decision, now)
        if not all(
            self.custody.verify(a) for m in session.manifests for a in m.artifacts
        ):
            raise IntegrityError("evidence integrity failed before handoff")
        event_id = "handoff:" + session.proposal.proposal_id
        handoff = self.repository.handoff_status(event_id)
        if handoff is None:
            raise IntegrityError("persisted proposal has no outbox intent")
        if handoff["publication_state"] != "sent":
            session.handoff_result = "delivery_pending"
            return session
        receipt = handoff["business_ack"]
        if (
            not isinstance(receipt, dict)
            or receipt.get("message_id") != event_id
            or receipt.get("status") != "received"
        ):
            raise IntegrityError("outbox status lacks a valid BC15 receipt")
        with bind_context(
            command_id=request.get("request_id"),
            session_id=session.session_id,
            event_id="handoff:" + session.proposal.proposal_id,
        ):
            log_event(
                _LOGGER,
                logging.INFO,
                "scout.handoff.received",
                state="received_by_bc15",
            )
        session.handoff_result = "received_by_bc15"
        session.complete(decision, now)
        self.repository.save(session, session.version)
        return session
