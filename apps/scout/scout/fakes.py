"""Single-process fakes: no production transaction or security guarantees."""

import copy
import hashlib
from threading import RLock

from .domain import ArtifactRef, AuthorizationError, ConflictError, IntegrityError


class FakeSourceLookup:
    def __init__(self, sources):
        self.sources = sources

    def resolve(self, source):
        if source not in self.sources:
            raise IntegrityError("source or revision not found")
        return self.sources[source]


class FakePolicy:
    def __init__(self, decision):
        self.decision = decision
        self.available = True

    def decide(self, principal, source, now):
        if not self.available or self.decision is None:
            raise AuthorizationError("policy unavailable")
        self.decision.check(principal, source, now)
        return self.decision


class FakeSessionRepository:
    def __init__(self):
        self.sessions = {}
        self.intents = {}
        self.outbox = {}
        self.custody_intents = {}
        self.custody_intent_sizes = {}
        # Serializes operations only inside this Python process.
        self._lock = RLock()

    def find_intent(self, principal, key, digest):
        with self._lock:
            record = self.intents.get((principal, key))
            if record is None:
                return None
            if record[0] != digest:
                raise ConflictError("idempotency key reused with different arguments")
            return copy.deepcopy(self.sessions[record[1]])

    def find_intent_header(self, principal, key, digest):
        with self._lock:
            record = self.intents.get((principal, key))
            if record is None:
                return None
            if record[0] != digest:
                raise ConflictError("idempotency key reused with different arguments")
            session = self.sessions[record[1]]
            return {"principal": session.principal, "source": session.source}

    def create(self, session, key, digest):
        with self._lock:
            if (
                session.principal,
                key,
            ) in self.intents or session.session_id in self.sessions:
                raise ConflictError("session or intent already exists")
            self.sessions[session.session_id] = copy.deepcopy(session)
            self.intents[(session.principal, key)] = (digest, session.session_id)
            self._queue_proposal(session)

    def save(self, session, expected_version):
        with self._lock:
            current = self.sessions.get(session.session_id)
            if (
                current is None
                or current.version != expected_version
                or session.version != expected_version
            ):
                raise ConflictError("optimistic concurrency conflict")
            committed = copy.deepcopy(session)
            committed.version = expected_version + 1
            self.sessions[session.session_id] = committed
            self._queue_proposal(committed)
            session.version = committed.version

    def _queue_proposal(self, session):
        if session.proposal is not None:
            event_id = "handoff:" + session.proposal.proposal_id
            self.outbox.setdefault(
                event_id,
                {
                    "proposal": session.proposal,
                    "publication_state": "pending",
                    "transport_receipt": None,
                    "business_ack": None,
                },
            )

    def claim_handoff(self, owner, lease_seconds, now):
        for event_id, event in self.outbox.items():
            if event["publication_state"] in {
                "pending",
                "uncertain",
                "retry_scheduled",
            } or (
                event["publication_state"] == "leased"
                and event.get("lease_deadline") <= now
            ):
                event["publication_state"] = "leased"
                event["lease_owner"] = owner
                event["lease_deadline"] = now
                event["fencing_token"] = event.get("fencing_token", 0) + 1
                event["attempt_count"] = event.get("attempt_count", 0) + 1
                return {
                    "event_id": event_id,
                    "payload": event["proposal"],
                    "attempt_count": event["attempt_count"],
                    "fencing_token": event["fencing_token"],
                }
        return None

    def finish_handoff(
        self,
        event_id,
        owner,
        fencing_token,
        *,
        status,
        error_class,
        result,
        next_attempt_delay_seconds,
        receipt=None,
    ):
        event = self.outbox[event_id]
        if (event.get("lease_owner"), event.get("fencing_token")) != (
            owner,
            fencing_token,
        ):
            return False
        event.update(
            publication_state=status,
            lease_owner=None,
            lease_deadline=None,
            error_class=error_class,
            last_result=result,
            transport_receipt=receipt,
        )
        if status == "sent":
            event["business_ack"] = receipt
        return True

    def handoff_status(self, event_id):
        event = self.outbox.get(event_id)
        if event is None:
            return None
        return {
            "publication_state": event["publication_state"],
            "business_ack": event.get("business_ack"),
            "attempt_count": event.get("attempt_count", 0),
        }

    def begin_custody_intent(self, artifact_id, session_id, sha256, size_bytes=None):
        intent = (session_id, sha256)
        previous = self.custody_intents.get(artifact_id)
        if previous is not None and previous[0:2] != intent:
            raise ConflictError("custody effect identity reused with different content")
        self.custody_intents.setdefault(artifact_id, (*intent, "pending", None))
        self.custody_intent_sizes.setdefault(artifact_id, size_bytes)

    def mark_custody_unknown(self, artifact_id):
        session_id, digest, _, ref = self.custody_intents[artifact_id]
        self.custody_intents[artifact_id] = (session_id, digest, "unknown", ref)

    def confirm_custody_intent(self, artifact_id, sha256, artifact_ref):
        session_id, digest, _, _ = self.custody_intents[artifact_id]
        if (
            digest != sha256
            or artifact_ref.get("artifact_id") != artifact_id
            or artifact_ref.get("sha256") != sha256
        ):
            raise IntegrityError("custody receipt identity or digest mismatch")
        self.custody_intents[artifact_id] = (
            session_id,
            digest,
            "verified",
            artifact_ref,
        )

    def get_custody_intent(self, artifact_id):
        record = self.custody_intents.get(artifact_id)
        if record is None:
            return None
        session = next(
            (s for s in self.sessions.values() if s.session_id == record[0]), None
        )
        return dict(zip(("session_id", "sha256", "status", "artifact_ref"), record)) | {
            "artifact_id": artifact_id,
            "source": session.source if session else None,
            "expected_size_bytes": self.custody_intent_sizes.get(artifact_id),
        }

    def mark_custody_discrepancy(self, artifact_id, note):
        session_id, digest, _, ref = self.custody_intents[artifact_id]
        self.custody_intents[artifact_id] = (session_id, digest, "manual_review", ref)

    def record_orphan_review(self, review_id, artifact_id, diagnostic):
        self.orphan_reviews = getattr(self, "orphan_reviews", {})
        self.orphan_reviews.setdefault(
            review_id,
            {
                "artifact_id": artifact_id,
                "owner": "BC06",
                "status": "manual_review",
                "diagnostic": copy.deepcopy(diagnostic),
            },
        )


class FakeEvidenceCustody:
    def __init__(self):
        self.objects = {}
        self.fail_before_store = False
        self.fail_after_store = False

    def store(self, session_id, source, content, artifact_id):
        if self.fail_before_store:
            raise OSError("simulated custody failure before write")
        digest = hashlib.sha256(content).hexdigest()
        artifact = ArtifactRef(
            artifact_id,
            session_id,
            source,
            digest,
            size_bytes=len(content),
            custody_status="available",
        )
        existing = self.objects.get(artifact.artifact_id)
        if existing is not None and existing != (artifact, content):
            raise ConflictError("artifact identity conflict")
        self.objects[artifact.artifact_id] = (artifact, bytes(content))
        if self.fail_after_store:
            raise OSError("simulated uncertain custody result after write")
        return artifact

    def verify(self, artifact):
        item = self.objects.get(artifact.artifact_id)
        return bool(
            item
            and item[0] == artifact
            and hashlib.sha256(item[1]).hexdigest() == artifact.sha256
        )

    def lookup(self, artifact_id):
        item = self.objects.get(artifact_id)
        return item[0] if item else None


class FakeProposalHandoff:
    def __init__(self):
        self.fail = False
        self.lose_ack = False
        self.deliveries = {}

    def deliver_message(self, message_id, payload):
        from .application import projection
        from .edge import validate

        if isinstance(payload, dict):
            validate("PlaybookProposal", payload)
            proposal_id = payload["proposal_id"]
        else:
            proposal_id = payload.proposal_id
            payload = projection(payload)
            validate("PlaybookProposal", payload)
        if self.fail:
            raise OSError("simulated handoff unavailable")
        previous = self.deliveries.get(message_id)
        if previous is not None and previous != payload:
            raise ConflictError("message identity conflict")
        self.deliveries[message_id] = payload
        if self.lose_ack:
            self.lose_ack = False
            raise OSError("simulated lost acknowledgement after receipt")
        return {
            "status": "received",
            "message_id": message_id,
            "proposal_id": proposal_id,
        }
