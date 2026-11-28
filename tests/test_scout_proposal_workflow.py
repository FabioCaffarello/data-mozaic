"""Proposal lifecycle binding tests use public application ports and fixtures."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scout.domain import AuthorizationError, IntegrityError, SourceRef
from scout.proposal_workflow import ProposalWorkflow

ROOT = Path(__file__).resolve().parents[1]


def transcript() -> dict:
    value = json.loads(
        (
            ROOT / "contracts/scout/transcript/draft/v2/fixtures/valid/multipage.json"
        ).read_text()
    )
    value["transcript_schema"] = "scout-transcript-draft/v3"
    value["pages"] = [value["pages"][0]]
    value["pages"][0]["content_type"] = "application/pdf"
    value["pages"][0]["discovered_links"] = []
    value["evidence"] = [value["evidence"][0]]
    value["findings"] = [
        {
            "classification": "observed",
            "evidence_refs": ["evidence-page-1"],
            "finding_id": "finding-catalog-1",
        }
    ]
    value["proposal"].update(
        finding_ids=["finding-catalog-1"],
        evidence_manifest_ids=["evidence-page-1"],
        required_capabilities=["http.fetch", "output.raw"],
    )
    value["navigation"]["observation_count"] = 1
    from collector_engineering.playbooks import build_raw_artifact_v4_draft

    candidate = build_raw_artifact_v4_draft(value)
    value["transcript_schema"] = "scout-transcript-draft/v4"
    value["proposal"]["contract_version"] = "2.0.0"
    candidate["playbook_id"] = "draft-proposal-session-1"
    candidate["proposal_ref"] = "proposal-session-1"
    value["proposal"].update(
        candidate_specification=candidate,
        candidate_sha256=hashlib.sha256(
            (
                json.dumps(
                    candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                + "\n"
            ).encode()
        ).hexdigest(),
        proposal_revision=1,
        proposal_id="proposal-session-1",
    )
    value["proposal_history"] = [
        {"revision": 1, "proposal": copy.deepcopy(value["proposal"])}
    ]
    return value


class ProposalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.value = transcript()

        class Scout:
            def export_transcript(_self, session_id, principal, now):
                if (session_id, principal) not in {
                    ("session-1", "analyst"),
                    ("session-1", "reviewer"),
                }:
                    raise PermissionError("not authorized")
                return copy.deepcopy(self.value)

            def export_transcript_for_review(_self, session_id, reviewer, now):
                if (session_id, reviewer) != (
                    "session-1",
                    "reviewer:controlled",
                ):
                    raise PermissionError("review scope denied")
                return copy.deepcopy(self.value)

        class Validator:
            def validate(_self, candidate):
                return ()

        class Authorizer:
            def authorize(_self, principal, proposal_id, revision):
                if principal != "reviewer" or revision != 1:
                    raise PermissionError("promotion denied")
                return "reviewer-test-1"

        class Registry:
            def __init__(_self):
                _self.calls = []

            def register(_self, candidate, *, actor_id):
                _self.calls.append((copy.deepcopy(candidate), actor_id))
                return {
                    "playbook_id": candidate["playbook_id"],
                    "playbook_digest": "sha256:" + "a" * 64,
                }

        self.registry = Registry()
        self.workflow = ProposalWorkflow(
            Scout(), Validator(), Authorizer(), self.registry
        )
        self.now = datetime.now(UTC) + timedelta(seconds=1)

    def test_validation_and_explicit_promotion_use_exact_candidate(self):
        result = self.workflow.validate("session-1", "analyst", self.now)
        self.assertTrue(result.valid)
        with self.assertRaises(PermissionError):
            self.workflow.promote(
                "session-1",
                "analyst",
                expected_candidate_sha256=result.candidate_sha256,
                now=self.now,
            )
        promoted = self.workflow.promote(
            "session-1",
            "reviewer",
            expected_candidate_sha256=result.candidate_sha256,
            now=self.now,
        )
        self.assertEqual("proposal-session-1", promoted["proposal_id"])
        self.assertEqual(
            self.value["proposal"]["candidate_specification"],
            self.registry.calls[-1][0],
        )

    def test_agent_host_workflow_validates_but_cannot_promote(self):
        from scout.tool_host.composition import _trusted_proposal_workflow

        agent_workflow = _trusted_proposal_workflow(self.workflow._scout)
        result = agent_workflow.validate("session-1", "analyst", self.now)
        self.assertTrue(result.valid, result.diagnostics)
        with self.assertRaisesRegex(AuthorizationError, "reviewer interface"):
            agent_workflow.promote(
                "session-1",
                "analyst",
                expected_candidate_sha256=result.candidate_sha256,
                now=self.now,
            )

    def test_cross_tenant_principal_cannot_validate_or_promote(self):
        with self.assertRaisesRegex(PermissionError, "not authorized"):
            self.workflow.validate("session-1", "other-tenant-agent", self.now)
        with self.assertRaisesRegex(PermissionError, "not authorized"):
            self.workflow.promote(
                "session-1",
                "other-tenant-reviewer",
                expected_candidate_sha256=self.value["proposal"]["candidate_sha256"],
                now=self.now,
            )
        self.assertEqual([], self.registry.calls)

    def test_changed_revision_after_validation_cannot_be_promoted(self):
        validated = self.workflow.validate("session-1", "analyst", self.now)
        self.value["proposal"]["candidate_specification"]["objective"] = (
            "changed after validation"
        )
        self._refresh_candidate_digest()
        with self.assertRaisesRegex(IntegrityError, "promotion intent"):
            self.workflow.promote(
                "session-1",
                "reviewer",
                expected_candidate_sha256=validated.candidate_sha256,
                now=self.now,
            )
        self.assertEqual([], self.registry.calls)

    def test_candidate_tamper_blocks_validation_and_promotion(self):
        self.value["proposal"]["candidate_specification"]["objective"] = "changed"
        with self.assertRaisesRegex(ValueError, "digest"):
            self.workflow.validate("session-1", "analyst", self.now)

    def test_candidate_outside_discovery_scope_is_rejected(self):
        self.value["approved_scope"]["path_prefixes"] = ["/catalog"]
        self.value["approval_record"]["path_prefixes"] = ["/catalog"]
        with self.assertRaisesRegex(IntegrityError, "scope"):
            self.workflow.validate("session-1", "analyst", self.now)

    def test_candidate_resource_budget_cannot_exceed_discovery_budget(self):
        candidate = self.value["proposal"]["candidate_specification"]
        candidate["budgets"]["max_response_bytes"] = 100_001
        candidate["output_contract"]["max_bytes"] = 100_001
        self._refresh_candidate_digest()
        with self.assertRaisesRegex(IntegrityError, "max_response_bytes"):
            self.workflow.validate("session-1", "analyst", self.now)

    def test_structured_candidate_does_not_require_artifact_byte_contract(self):
        candidate = self.value["proposal"]["candidate_specification"]
        candidate["output_contract"] = {
            "record_type": "object",
            "fields": {"id": {"type": "string", "required": True}},
        }
        self._refresh_candidate_digest()
        result = self.workflow.validate("session-1", "analyst", self.now)
        self.assertTrue(result.valid)

    def test_candidate_from_another_source_revision_is_rejected(self):
        candidate = self.value["proposal"]["candidate_specification"]
        candidate["source"]["revision"] = "source-rev-2"
        self._refresh_candidate_digest()
        with self.assertRaisesRegex(IntegrityError, "source revision"):
            self.workflow.validate("session-1", "analyst", self.now)

    def test_candidate_evidence_must_match_custodied_manifest(self):
        candidate = self.value["proposal"]["candidate_specification"]
        candidate["evidence"][0]["sha256"] = "0" * 64
        self._refresh_candidate_digest()
        with self.assertRaisesRegex(IntegrityError, "custody metadata"):
            self.workflow.validate("session-1", "analyst", self.now)

    def test_candidate_cannot_reference_evidence_outside_its_session_scope(self):
        candidate = self.value["proposal"]["candidate_specification"]
        candidate["evidence"][0]["evidence_ref"] = "manifest-from-another-tenant"
        candidate["findings"][0]["evidence_refs"] = ["manifest-from-another-tenant"]
        self._refresh_candidate_digest()
        with self.assertRaisesRegex(IntegrityError, "evidence scope"):
            self.workflow.validate("session-1", "analyst", self.now)

    def test_semantic_diagnostics_prevent_promotion(self):
        class RejectingValidator:
            def validate(_self, candidate):
                from scout.proposal_workflow import ProposalDiagnostic

                return (
                    ProposalDiagnostic(
                        "UNSUPPORTED_CAPABILITY",
                        "$.operations",
                        "candidate requests an unsupported operator",
                    ),
                )

        workflow = ProposalWorkflow(
            self.workflow._scout,
            RejectingValidator(),
            self.workflow._authorizers,
            self.registry,
        )
        result = workflow.validate("session-1", "analyst", self.now)
        self.assertFalse(result.valid)
        with self.assertRaisesRegex(IntegrityError, "invalid proposal"):
            workflow.promote(
                "session-1",
                "reviewer",
                expected_candidate_sha256=result.candidate_sha256,
                now=self.now,
            )
        self.assertEqual([], self.registry.calls)

    def test_review_promotion_revalidates_and_uses_authenticated_identity(self):
        from scout.reviewer_identity import (
            AuthenticatedReviewerAccess,
            AuthenticatedReviewerPromotion,
            ReviewerIdentity,
            ReviewerPromotionAdapter,
        )

        identity = ReviewerIdentity(
            7002,
            "reviewer:controlled",
            frozenset({("catalog", "rev-1")}),
        )
        access = AuthenticatedReviewerAccess(identity)
        source = SourceRef("catalog", "rev-1")
        access.authorize(identity.principal, source)
        workflow = ProposalWorkflow(
            self.workflow._scout,
            self.workflow._validator,
            AuthenticatedReviewerPromotion(identity),
            self.registry,
        )

        class Provider:
            def current(_self):
                return identity

        adapter = ReviewerPromotionAdapter(Provider(), workflow)
        validated = workflow.validate_for_review(
            "session-1", identity.principal, self.now
        )
        promoted = adapter.promote(
            "session-1",
            validated.proposal_id,
            validated.proposal_revision,
            validated.candidate_sha256,
            now=self.now,
        )
        self.assertEqual(validated.candidate_sha256, promoted["candidate_sha256"])
        self.assertEqual("reviewer:controlled", self.registry.calls[-1][1])

    def test_review_promotion_rejects_source_scope_and_candidate_tampering(self):
        from scout.reviewer_identity import (
            AuthenticatedReviewerAccess,
            AuthenticatedReviewerPromotion,
            ReviewerIdentity,
        )

        identity = ReviewerIdentity(7002, "reviewer:controlled", frozenset())
        with self.assertRaises(AuthorizationError):
            AuthenticatedReviewerAccess(identity).authorize(
                identity.principal, SourceRef("catalog", "rev-1")
            )
        workflow = ProposalWorkflow(
            self.workflow._scout,
            self.workflow._validator,
            AuthenticatedReviewerPromotion(identity),
            self.registry,
        )
        validated = workflow.validate_for_review(
            "session-1", identity.principal, self.now
        )
        self.value["proposal"]["candidate_specification"]["objective"] = "changed"
        self._refresh_candidate_digest()
        with self.assertRaisesRegex(IntegrityError, "promotion intent"):
            workflow.promote_for_review(
                "session-1",
                identity.principal,
                expected_proposal_id=validated.proposal_id,
                expected_proposal_revision=validated.proposal_revision,
                expected_candidate_sha256=validated.candidate_sha256,
                now=self.now,
            )
        self.assertEqual([], self.registry.calls)

    def test_reviewer_handoff_must_name_exact_proposal_revision(self):
        from scout.reviewer_identity import (
            AuthenticatedReviewerPromotion,
            ReviewerIdentity,
        )

        identity = ReviewerIdentity(
            7002,
            "reviewer:controlled",
            frozenset({("catalog", "rev-1")}),
        )
        workflow = ProposalWorkflow(
            self.workflow._scout,
            self.workflow._validator,
            AuthenticatedReviewerPromotion(identity),
            self.registry,
        )
        old_validation = workflow.validate_for_review(
            "session-1", identity.principal, self.now
        )
        # A proposal revision may reuse the candidate while changing context.
        # A stale handoff must still be rejected.
        revised = copy.deepcopy(self.value["proposal"])
        revised["proposal_revision"] = 2
        revised["risks"] = ["new reviewer context"]
        self.value["proposal"] = revised
        self.value["proposal_history"].append(
            {"revision": 2, "proposal": copy.deepcopy(revised)}
        )
        with self.assertRaisesRegex(IntegrityError, "proposal revision"):
            workflow.promote_for_review(
                "session-1",
                identity.principal,
                expected_proposal_id=old_validation.proposal_id,
                expected_proposal_revision=old_validation.proposal_revision,
                expected_candidate_sha256=old_validation.candidate_sha256,
                now=self.now,
            )
        self.assertEqual([], self.registry.calls)

    def test_proposal_cannot_reference_an_unrelated_finding(self):
        candidate = self.value["proposal"]["candidate_specification"]
        candidate["findings"][0]["finding_id"] = "finding-from-another-source"
        self._refresh_candidate_digest()
        with self.assertRaisesRegex(IntegrityError, "evidence scope"):
            self.workflow.validate("session-1", "analyst", self.now)

    def test_reviewer_cannot_be_supplied_as_agent_request_field(self):
        from scout.reviewer_identity import (
            AuthenticatedReviewerPromotion,
            ReviewerIdentity,
        )

        identity = ReviewerIdentity(
            7002,
            "reviewer:controlled",
            frozenset({("catalog", "rev-1")}),
        )
        authorizer = AuthenticatedReviewerPromotion(identity)
        with self.assertRaisesRegex(AuthorizationError, "does not match reviewer"):
            authorizer.authorize("analyst", "proposal-session-1", 1)

    def _refresh_candidate_digest(self):
        candidate = self.value["proposal"]["candidate_specification"]
        canonical = (
            json.dumps(
                candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode()
        self.value["proposal"]["candidate_sha256"] = hashlib.sha256(
            canonical
        ).hexdigest()
        self.value["proposal_history"][0]["proposal"] = copy.deepcopy(
            self.value["proposal"]
        )


if __name__ == "__main__":
    unittest.main()
