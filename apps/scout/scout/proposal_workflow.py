"""BC14 proposal validation and explicit BC15/M4 handoff composition.

This module coordinates public ports only. BC14 verifies proposal/evidence
binding, the supplied BC15 validator owns Playbook semantics, and M4 owns
immutable Playbook registration. This operation does not approve a release.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from .application import ScoutApplication
from .domain import IntegrityError
from .edge import validate_transcript


@dataclass(frozen=True)
class ProposalDiagnostic:
    code: str
    path: str
    message: str


@dataclass(frozen=True)
class ProposalValidation:
    proposal_id: str
    proposal_revision: int
    candidate_sha256: str
    valid: bool
    diagnostics: tuple[ProposalDiagnostic, ...]


class CandidateValidatorPort(Protocol):
    def validate(self, candidate: dict[str, Any]) -> tuple[ProposalDiagnostic, ...]: ...


class PromotionAuthorizationPort(Protocol):
    def authorize(self, principal: str, proposal_id: str, revision: int) -> str: ...


class PlaybookRegistrationPort(Protocol):
    def register(
        self, candidate: dict[str, Any], *, actor_id: str
    ) -> dict[str, str]: ...


def _candidate_digest(candidate: dict[str, Any]) -> str:
    canonical = (
        json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _within_path(path: str, prefix: str) -> bool:
    normalized = prefix.rstrip("/") or "/"
    return normalized == "/" or path == normalized or path.startswith(normalized + "/")


class ProposalWorkflow:
    """Revalidate an authorized Scout snapshot before validation or promotion."""

    def __init__(
        self,
        scout: ScoutApplication,
        validator: CandidateValidatorPort,
        authorizers: PromotionAuthorizationPort,
        playbooks: PlaybookRegistrationPort,
    ):
        self._scout = scout
        self._validator = validator
        self._authorizers = authorizers
        self._playbooks = playbooks

    def validate(
        self, session_id: str, principal: str, now: datetime
    ) -> ProposalValidation:
        # export_transcript reauthorizes the caller and independently verifies
        # every referenced BC06 artifact before it returns a DTO.
        transcript = self._scout.export_transcript(session_id, principal, now)
        return self._validate_transcript(transcript)

    def validate_for_review(
        self, session_id: str, reviewer: str, now: datetime
    ) -> ProposalValidation:
        """Validate a proposal using BC14's source-scoped reviewer read path."""
        transcript = self._scout.export_transcript_for_review(session_id, reviewer, now)
        return self._validate_transcript(transcript)

    def _validate_transcript(self, transcript: dict[str, Any]) -> ProposalValidation:
        validate_transcript(transcript)
        proposal = transcript["proposal"]
        if proposal.get("contract_version") != "2.0.0":
            raise IntegrityError("proposal has no declarative candidate (v2 required)")
        candidate = proposal["candidate_specification"]
        digest = _candidate_digest(candidate)
        if digest != proposal.get("candidate_sha256"):
            raise IntegrityError("candidate specification digest mismatch")
        source = transcript["session"]["source"]
        approved_scope = transcript["approved_scope"]
        if (
            candidate.get("source", {}).get("source_id") != source["source_id"]
            or candidate.get("source", {}).get("revision") != source["revision"]
        ):
            raise IntegrityError("candidate source revision differs from Scout session")
        if candidate.get("proposal_ref") != proposal["proposal_id"]:
            raise IntegrityError("candidate does not bind the exact proposal identity")
        candidate_source = candidate.get("source", {})
        approved_prefixes = approved_scope.get("path_prefixes", [])
        if (
            candidate_source.get("origin") != approved_scope.get("origin")
            or not candidate_source.get("path_prefixes")
            or any(
                not any(
                    _within_path(prefix, approved) for approved in approved_prefixes
                )
                for prefix in candidate_source["path_prefixes"]
            )
        ):
            raise IntegrityError(
                "candidate scope exceeds the authorized discovery scope"
            )
        # A proposal is not execution authorization, but it must not claim a
        # broader resource envelope than the discovery session was granted.
        # The later SourceRevision, policy, release, and component limits still
        # independently govern execution.
        approved_budgets = approved_scope.get("budgets", {})
        candidate_budgets = candidate.get("budgets", {})
        for key in ("max_requests", "max_pages", "max_response_bytes"):
            approved_limit = approved_budgets.get(key)
            candidate_limit = candidate_budgets.get(key)
            if (
                not isinstance(approved_limit, int)
                or isinstance(approved_limit, bool)
                or not isinstance(candidate_limit, int)
                or isinstance(candidate_limit, bool)
                or candidate_limit < 1
                or candidate_limit > approved_limit
            ):
                raise IntegrityError(
                    f"candidate {key} exceeds or lacks the authorized discovery budget"
                )
        output_contract = candidate.get("output_contract", {})
        if output_contract.get("mode") == "artifact":
            output_limit = output_contract.get("max_bytes")
            if (
                not isinstance(output_limit, int)
                or isinstance(output_limit, bool)
                or output_limit < 1
                or output_limit > approved_budgets["max_response_bytes"]
            ):
                raise IntegrityError(
                    "candidate artifact byte limit exceeds discovery response budget"
                )
        evidence_ids = set(proposal["evidence_manifest_ids"])
        candidate_findings = candidate.get("findings", [])
        declared_findings = set(proposal["finding_ids"])
        transcript_findings = {
            finding["finding_id"]: set(finding["evidence_refs"])
            for finding in transcript["findings"]
        }
        candidate_finding_ids = {
            finding.get("finding_id")
            for finding in candidate_findings
            if isinstance(finding, dict)
        }
        candidate_refs = {
            ref
            for finding in candidate_findings
            if isinstance(finding, dict)
            for ref in finding.get("evidence_refs", [])
        }
        if (
            not candidate_refs
            or not candidate_refs <= evidence_ids
            or not candidate_finding_ids
            or not candidate_finding_ids <= declared_findings
            or any(
                not set(finding.get("evidence_refs", []))
                <= transcript_findings.get(finding.get("finding_id"), set())
                for finding in candidate_findings
                if isinstance(finding, dict)
            )
            or any(
                split.scheme != "https"
                or split.netloc != urlsplit(approved_scope["origin"]).netloc
                or not any(
                    _within_path(split.path, prefix) for prefix in approved_prefixes
                )
                for operation in candidate.get("operations", [])
                if isinstance(operation, dict) and isinstance(operation.get("url"), str)
                for split in (urlsplit(operation["url"]),)
            )
        ):
            raise IntegrityError("candidate findings escape proposal evidence scope")
        transcript_artifacts = transcript["evidence"]
        for evidence in candidate.get("evidence", []):
            if not any(
                artifact["manifest_id"] == evidence.get("evidence_ref")
                and artifact["sha256"] == evidence.get("sha256")
                for artifact in transcript_artifacts
            ):
                raise IntegrityError(
                    "candidate evidence digest differs from custody metadata"
                )
        diagnostics = self._validator.validate(candidate)
        return ProposalValidation(
            proposal["proposal_id"],
            proposal["proposal_revision"],
            digest,
            not diagnostics,
            tuple(diagnostics),
        )

    def promote(
        self,
        session_id: str,
        principal: str,
        *,
        expected_candidate_sha256: str,
        now: datetime,
    ) -> dict[str, str]:
        validation = self.validate(session_id, principal, now)
        transcript = self._scout.export_transcript(session_id, principal, now)
        return self._promote_validated(
            transcript, principal, validation, expected_candidate_sha256
        )

    def promote_for_review(
        self,
        session_id: str,
        reviewer: str,
        *,
        expected_proposal_id: str,
        expected_proposal_revision: int,
        expected_candidate_sha256: str,
        now: datetime,
    ) -> dict[str, str]:
        """Promote only after reviewer authentication and fresh BC14/BC15 checks."""
        validation = self.validate_for_review(session_id, reviewer, now)
        if (
            validation.proposal_id != expected_proposal_id
            or validation.proposal_revision != expected_proposal_revision
        ):
            raise IntegrityError("promotion intent differs from proposal revision")
        transcript = self._scout.export_transcript_for_review(session_id, reviewer, now)
        return self._promote_validated(
            transcript, reviewer, validation, expected_candidate_sha256
        )

    def _promote_validated(
        self,
        transcript: dict[str, Any],
        principal: str,
        validation: ProposalValidation,
        expected_candidate_sha256: str,
    ) -> dict[str, str]:
        if validation.candidate_sha256 != expected_candidate_sha256:
            raise IntegrityError("promotion intent differs from validated candidate")
        if not validation.valid:
            raise IntegrityError("invalid proposal cannot be promoted")
        proposal = transcript["proposal"]
        if (
            proposal.get("proposal_id") != validation.proposal_id
            or proposal.get("proposal_revision") != validation.proposal_revision
            or _candidate_digest(proposal.get("candidate_specification", {}))
            != expected_candidate_sha256
        ):
            raise IntegrityError("proposal changed between validation and promotion")
        actor_id = self._authorizers.authorize(
            principal, proposal["proposal_id"], proposal["proposal_revision"]
        )
        # M4's immutable (playbook_id, content digest) registration is
        # idempotent. A retry resolves the same content identity; no release
        # approval or execution authority is installed here.
        result = self._playbooks.register(
            proposal["candidate_specification"], actor_id=actor_id
        )
        if (
            not isinstance(result, dict)
            or not result.get("playbook_id")
            or not result.get("playbook_digest")
        ):
            raise IntegrityError("M4 did not return an immutable Playbook identity")
        return {
            "proposal_id": proposal["proposal_id"],
            "proposal_revision": str(proposal["proposal_revision"]),
            "candidate_sha256": validation.candidate_sha256,
            "playbook_id": result["playbook_id"],
            "playbook_digest": result["playbook_digest"],
        }
