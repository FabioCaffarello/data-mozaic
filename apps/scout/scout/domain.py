"""BC14 language and invariants; no infrastructure dependencies."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class ScoutError(Exception):
    """Base for expected domain failures."""


class AuthorizationError(ScoutError):
    pass


class TransitionError(ScoutError):
    pass


class IntegrityError(ScoutError):
    pass


class ConflictError(ScoutError):
    pass


class SourceRevisionConflict(ConflictError):
    """The fixed source revision no longer resolves to its authorized scope."""


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    revision: str


@dataclass(frozen=True)
class ScoutObjective:
    text: str

    def __post_init__(self):
        if not self.text.strip():
            raise IntegrityError("empty objective")


@dataclass(frozen=True)
class ScoutScope:
    origins: tuple[str, ...]
    path_prefixes: tuple[str, ...]
    methods: tuple[str, ...]
    max_requests: int
    max_duration_seconds: int

    def __post_init__(self):
        if (
            not all((self.origins, self.path_prefixes, self.methods))
            or min(self.max_requests, self.max_duration_seconds) < 1
        ):
            raise IntegrityError("empty scope or budget")

    def intersect(self, other: "ScoutScope") -> "ScoutScope":
        # Prefix containment is path-segment aware: /foo does not contain /foobar.
        paths = tuple(
            sorted(
                {
                    a if _path_prefix_contains(b, a) else b
                    for a in self.path_prefixes
                    for b in other.path_prefixes
                    if _path_prefix_contains(a, b) or _path_prefix_contains(b, a)
                }
            )
        )
        return ScoutScope(
            tuple(sorted(set(self.origins) & set(other.origins))),
            paths,
            tuple(sorted(set(self.methods) & set(other.methods))),
            min(self.max_requests, other.max_requests),
            min(self.max_duration_seconds, other.max_duration_seconds),
        )

    def allows(self, origin: str, path: str, method: str = "GET") -> bool:
        """Check a normalized target against this already-authorized scope."""
        return (
            origin in self.origins
            and method in self.methods
            and any(
                _path_prefix_contains(prefix, path) for prefix in self.path_prefixes
            )
        )


def _path_prefix_contains(prefix: str, path: str) -> bool:
    normalized = prefix.rstrip("/") or "/"
    if normalized == "/":
        return path.startswith("/")
    return path == normalized or path.startswith(normalized + "/")


@dataclass(frozen=True)
class PolicyDecisionRef:
    decision_id: str
    principal: str
    source: SourceRef
    allowed: bool
    expires_at: datetime
    scope: ScoutScope
    revoked: bool = False
    max_response_bytes: int = 2_000_000

    def check(self, principal: str, source: SourceRef, now: datetime):
        if (
            not self.decision_id
            or not self.allowed
            or self.revoked
            or self.principal != principal
            or self.source != source
            or self.expires_at <= now
        ):
            raise AuthorizationError(
                "policy decision invalid, denied, expired or revoked"
            )


@dataclass(frozen=True)
class AuthorizedNavigationScope:
    scope: ScoutScope
    decision_id: str


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    session_id: str
    source: SourceRef
    sha256: str
    kind: str = "other"
    redaction: str = "redacted"
    size_bytes: int | None = None
    custody_status: str | None = None


@dataclass(frozen=True)
class SourceObservation:
    observation_id: str
    session_id: str
    source: SourceRef
    statement: str
    artifact_id: str
    url: str | None = None
    status: int | None = None
    content_type: str | None = None
    title: str | None = None
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceManifest:
    manifest_id: str
    session_id: str
    source: SourceRef
    sealed_at: str
    artifacts: tuple[ArtifactRef, ...]


@dataclass(frozen=True)
class DiscoveryFinding:
    finding_id: str
    session_id: str
    source: SourceRef
    statement: str
    epistemic_status: str
    confidence: float
    manifest_ids: tuple[str, ...]
    verification_note: str


@dataclass(frozen=True)
class PlaybookProposal:
    proposal_id: str
    session_id: str
    source: SourceRef
    summary: str
    finding_ids: tuple[str, ...]
    manifest_ids: tuple[str, ...]
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]
    required_capabilities: tuple[str, ...]


class State(StrEnum):
    REQUESTED = "requested"
    AUTHORIZED = "authorized"
    EXPLORING = "exploring"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class ScoutSession:
    session_id: str
    source: SourceRef
    objective: ScoutObjective
    requested_scope: ScoutScope
    principal: str
    state: State = State.REQUESTED
    version: int = 0
    authorization: AuthorizedNavigationScope | None = None
    remaining_requests: int = 0
    remaining_seconds: int = 0
    observations: tuple[SourceObservation, ...] = field(default_factory=tuple)
    manifests: tuple[EvidenceManifest, ...] = field(default_factory=tuple)
    findings: tuple[DiscoveryFinding, ...] = field(default_factory=tuple)
    proposal: PlaybookProposal | None = None
    handoff_result: str | None = None

    def __setattr__(self, name, value):
        if (
            name
            in {"session_id", "source", "objective", "requested_scope", "principal"}
            and name in self.__dict__
        ):
            raise IntegrityError(
                "session identity, objective and requested scope are fixed"
            )
        if name != "version" and self.__dict__.get("state") in {
            State.COMPLETED,
            State.CANCELLED,
            State.FAILED,
        }:
            raise TransitionError("terminal session is immutable")
        super().__setattr__(name, value)

    def _state(self, *allowed: State):
        if self.state not in allowed:
            raise TransitionError(f"{self.state}: operation not allowed")

    def _policy(self, decision: PolicyDecisionRef, now: datetime):
        if decision is None:
            raise AuthorizationError("policy unavailable")
        decision.check(self.principal, self.source, now)
        if (
            not self.authorization
            or decision.decision_id != self.authorization.decision_id
        ):
            raise AuthorizationError("decision does not authorize this session")
        # A changed policy cannot silently widen or invalidate the granted scope.
        if (
            self.authorization.scope.intersect(decision.scope)
            != self.authorization.scope
        ):
            raise AuthorizationError("policy scope changed")

    def authorize(
        self, source_scope: ScoutScope, decision: PolicyDecisionRef, now: datetime
    ):
        self._state(State.REQUESTED, State.PAUSED)
        if decision is None:
            raise AuthorizationError("policy unavailable")
        decision.check(self.principal, self.source, now)
        scope = self.requested_scope.intersect(source_scope).intersect(decision.scope)
        if self.state == State.PAUSED:
            scope = scope.intersect(self.authorization.scope)
            self.remaining_requests = min(self.remaining_requests, scope.max_requests)
            self.remaining_seconds = min(
                self.remaining_seconds, scope.max_duration_seconds
            )
        else:
            self.remaining_requests = scope.max_requests
            self.remaining_seconds = scope.max_duration_seconds
        self.authorization = AuthorizedNavigationScope(scope, decision.decision_id)
        self.state = State.AUTHORIZED

    def start(self, decision: PolicyDecisionRef, now: datetime):
        self._state(State.AUTHORIZED)
        self._policy(decision, now)
        self.state = State.EXPLORING

    def pause(self):
        self._state(State.EXPLORING)
        self.state = State.PAUSED

    def cancel(self):
        self._state(State.REQUESTED, State.AUTHORIZED, State.EXPLORING, State.PAUSED)
        self.state = State.CANCELLED

    def fail(self):
        self._state(State.REQUESTED, State.AUTHORIZED, State.EXPLORING, State.PAUSED)
        self.state = State.FAILED

    def observe(
        self,
        observation: SourceObservation,
        decision: PolicyDecisionRef,
        now: datetime,
        elapsed_seconds: int = 1,
    ):
        self._state(State.EXPLORING)
        self._policy(decision, now)
        if (
            observation.session_id != self.session_id
            or observation.source != self.source
            or not observation.statement.strip()
            or not observation.artifact_id
        ):
            raise IntegrityError("invalid observation")
        if (
            elapsed_seconds < 0
            or self.remaining_requests < 1
            or self.remaining_seconds < elapsed_seconds
        ):
            raise IntegrityError("budget exhausted")
        if any(
            o.observation_id == observation.observation_id for o in self.observations
        ):
            raise IntegrityError("duplicate observation")
        self.remaining_requests -= 1
        self.remaining_seconds -= elapsed_seconds
        self.observations += (observation,)

    def seal(
        self,
        manifest: EvidenceManifest,
        verified: bool,
        decision: PolicyDecisionRef,
        now: datetime,
    ):
        self._state(State.EXPLORING)
        self._policy(decision, now)
        if (
            not verified
            or manifest.session_id != self.session_id
            or manifest.source != self.source
            or not manifest.artifacts
            or any(
                a.session_id != self.session_id
                or a.source != self.source
                or not any(o.artifact_id == a.artifact_id for o in self.observations)
                for a in manifest.artifacts
            )
            or any(m.manifest_id == manifest.manifest_id for m in self.manifests)
        ):
            raise IntegrityError("invalid evidence manifest")
        self.manifests += (manifest,)

    def add_finding(
        self, finding: DiscoveryFinding, decision: PolicyDecisionRef, now: datetime
    ):
        self._state(State.EXPLORING)
        self._policy(decision, now)
        if (
            finding.session_id != self.session_id
            or finding.source != self.source
            or not finding.statement.strip()
            or not finding.verification_note.strip()
            or finding.epistemic_status not in ("observation", "hypothesis")
            or not 0 <= finding.confidence <= 1
            or not finding.manifest_ids
            or not set(finding.manifest_ids) <= {m.manifest_id for m in self.manifests}
            or any(f.finding_id == finding.finding_id for f in self.findings)
        ):
            raise IntegrityError(
                "invalid finding; independent verification is not implemented"
            )
        self.findings += (finding,)

    def propose(
        self, proposal: PlaybookProposal, decision: PolicyDecisionRef, now: datetime
    ):
        self._state(State.EXPLORING)
        self._policy(decision, now)
        if (
            self.proposal is not None
            or proposal.session_id != self.session_id
            or proposal.source != self.source
            or not proposal.summary.strip()
            or not proposal.finding_ids
            or not proposal.manifest_ids
            or not set(proposal.finding_ids) <= {f.finding_id for f in self.findings}
            or not set(proposal.manifest_ids) <= {m.manifest_id for m in self.manifests}
            or not set(proposal.manifest_ids)
            <= {
                m
                for f in self.findings
                if f.finding_id in proposal.finding_ids
                for m in f.manifest_ids
            }
        ):
            raise IntegrityError("invalid proposal")
        self.proposal = proposal

    def complete(self, decision: PolicyDecisionRef, now: datetime):
        self._state(State.EXPLORING)
        self._policy(decision, now)
        if (
            not self.manifests
            or not self.findings
            or not self.proposal
            or self.handoff_result != "received_by_bc15"
        ):
            raise IntegrityError("incomplete evidence or handoff")
        self.state = State.COMPLETED
