"""Offline application workflow; validation of public JSON belongs at the edge."""

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime

from mozaic_observability import bind_context, log_event

from .domain import (
    DiscoveryFinding,
    EvidenceManifest,
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
    PolicyDecisionPort,
    ScoutSessionRepositoryPort,
    SourceLookupPort,
)
from .projections import projection  # noqa: F401 -- preserve the application import path

_LOGGER = logging.getLogger(__name__)


class ScoutApplication:
    def __init__(
        self,
        sources: SourceLookupPort,
        policy: PolicyDecisionPort,
        repository: ScoutSessionRepositoryPort,
        custody: EvidenceCustodyPort,
    ):
        self.sources, self.policy, self.repository = sources, policy, repository
        self.custody = custody

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
