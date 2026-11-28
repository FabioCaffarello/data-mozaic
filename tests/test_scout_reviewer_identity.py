"""Reviewer identity is derived from a protected OS identity mapping."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from scout.domain import AuthorizationError, SourceRef
from scout.reviewer_identity import (
    AuthenticatedReviewerAccess,
    AuthenticatedReviewerPromotion,
    ReviewerPromotionAdapter,
    UnixReviewerIdentityProvider,
)


class ReviewerIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.policy = Path(self.temp.name) / "reviewers.json"
        self.document = {
            "version": 1,
            "agent_uid": 7001,
            "reviewers": [
                {
                    "uid": 7002,
                    "principal": "reviewer:controlled",
                    "source_scopes": [{"source_id": "catalog", "revision": "rev-1"}],
                }
            ],
        }
        self._write()

    def tearDown(self):
        self.temp.cleanup()

    def _write(self):
        if self.policy.exists():
            self.policy.chmod(0o600)
        self.policy.write_text(json.dumps(self.document), encoding="utf-8")
        self.policy.chmod(0o440)

    def _provider(self, uid=(7002, 7002)):
        return UnixReviewerIdentityProvider(
            self.policy,
            uid_provider=lambda: uid,
            expected_owner_uid=os.geteuid(),
        )

    def test_identity_is_derived_from_os_uid_and_exact_source_scope(self):
        identity = self._provider().current()
        self.assertEqual("reviewer:controlled", identity.principal)
        access = AuthenticatedReviewerAccess(identity)
        access.authorize(identity.principal, SourceRef("catalog", "rev-1"))
        with self.assertRaises(AuthorizationError):
            access.authorize(identity.principal, SourceRef("catalog", "rev-2"))

    def test_agent_uid_forged_principal_and_unmapped_user_are_denied(self):
        with self.assertRaisesRegex(AuthorizationError, "agent identity"):
            self._provider((7001, 7001)).current()
        with self.assertRaisesRegex(AuthorizationError, "not an authorized reviewer"):
            self._provider((7003, 7003)).current()
        identity = self._provider().current()
        with self.assertRaisesRegex(AuthorizationError, "does not match reviewer"):
            AuthenticatedReviewerPromotion(identity).authorize(
                "reviewer:forged", "proposal-1", 1
            )

    def test_policy_rejects_forged_role_fields_and_unsafe_permissions(self):
        self.document["reviewers"][0]["role"] = "reviewer"
        self._write()
        with self.assertRaisesRegex(AuthorizationError, "policy is invalid"):
            self._provider().current()
        self.document["reviewers"][0].pop("role")
        self._write()
        self.policy.chmod(0o660)
        with self.assertRaisesRegex(AuthorizationError, "not protected"):
            self._provider().current()

    def test_reviewer_cli_adapter_never_accepts_actor_from_request(self):
        identity = self._provider().current()

        class Provider:
            def current(_self):
                return identity

        class Workflow:
            call = None

            def promote_for_review(_self, session_id, reviewer, **kwargs):
                _self.call = (session_id, reviewer, kwargs)
                return {"playbook_id": "playbook-1"}

        workflow = Workflow()
        adapter = ReviewerPromotionAdapter(Provider(), workflow)
        outcome = adapter.promote(
            "session-1",
            "proposal-1",
            3,
            "a" * 64,
            now=datetime.now(UTC),
        )
        self.assertEqual({"playbook_id": "playbook-1"}, outcome)
        self.assertEqual("reviewer:controlled", workflow.call[1])
        self.assertEqual("proposal-1", workflow.call[2]["expected_proposal_id"])
        self.assertEqual(3, workflow.call[2]["expected_proposal_revision"])
        self.assertEqual("a" * 64, workflow.call[2]["expected_candidate_sha256"])


if __name__ == "__main__":
    unittest.main()
