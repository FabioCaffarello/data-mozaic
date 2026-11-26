from __future__ import annotations

import base64
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.execution_capability import (
    ExecutionCapabilitySigner,
    verify_execution_capability,
)
from execution_coordination.domain import CoordinationError
from execution_coordination.durable_domain import AttemptLease
from jsonschema import Draft202012Validator, FormatChecker


class ExecutionCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 25, 16, 0, tzinfo=UTC)
        self.seed = b"c" * 32
        self.private_key = Ed25519PrivateKey.from_private_bytes(self.seed)
        self.public_key = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.lease = AttemptLease(
            tenant_id="tenant-controlled",
            run_id=str(uuid4()),
            attempt_id=str(uuid4()),
            generation=3,
            lease_owner="coordinator-1",
            lease_token="private-lease-token-not-in-capability",
            lease_until=self.now + timedelta(seconds=90),
            attempt_deadline=self.now + timedelta(seconds=100),
        )
        self.signer = ExecutionCapabilitySigner(self.seed, key_id="bc04-live-1")

    def _issue(self, **overrides):
        fields = {
            "source_id": "controlled-http",
            "source_revision_id": "controlled-v1",
            "policy_id": "controlled-get",
            "policy_version": "1.0.0",
            "policy_digest": "sha256:" + "a" * 64,
            "release_id": "release-controlled-1",
            "build_id": "sha256:" + "b" * 64,
            "build_manifest_digest": "sha256:" + "b" * 64,
            "runtime_profile_id": "scrapy-live-http-v1",
            "audience": "acquisition-gateway/local-test",
            "execution_deadline": self.now + timedelta(seconds=120),
            "now": self.now,
        }
        fields.update(overrides)
        return self.signer.issue(self.lease, **fields)

    def test_capability_is_signed_scoped_and_schema_valid(self):
        capability = self._issue()
        schema_path = (
            Path(__file__).parents[3]
            / "contracts/execution-capability/draft/v1/ExecutionCapability.schema.json"
        )
        Draft202012Validator(
            json.loads(schema_path.read_text()), format_checker=FormatChecker()
        ).validate(capability)
        verified = verify_execution_capability(
            capability,
            trusted_public_keys={"bc04-live-1": self.public_key},
            expected_audience="acquisition-gateway/local-test",
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(verified["tenant_id"], self.lease.tenant_id)
        self.assertEqual(verified["attempt_id"], self.lease.attempt_id)
        self.assertEqual(verified["generation"], 3)
        self.assertNotIn("lease_token", capability)

    def test_work_intake_parameter_digest_is_bound_and_schema_valid(self):
        digest = "sha256:" + "e" * 64
        capability = self._issue(input_parameters_digest=digest)
        schema_path = (
            Path(__file__).parents[3]
            / "contracts/execution-capability/draft/v1/ExecutionCapability.schema.json"
        )
        Draft202012Validator(
            json.loads(schema_path.read_text()), format_checker=FormatChecker()
        ).validate(capability)
        verified = verify_execution_capability(
            capability,
            trusted_public_keys={"bc04-live-1": self.public_key},
            expected_audience="acquisition-gateway/local-test",
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(verified["input_parameters_digest"], digest)

    def test_work_intake_initial_target_is_bound_and_schema_valid(self):
        digest = "sha256:" + "f" * 64
        capability = self._issue(expected_initial_target_digest=digest)
        schema_path = (
            Path(__file__).parents[3]
            / "contracts/execution-capability/draft/v1/ExecutionCapability.schema.json"
        )
        Draft202012Validator(
            json.loads(schema_path.read_text()), format_checker=FormatChecker()
        ).validate(capability)
        verified = verify_execution_capability(
            capability,
            trusted_public_keys={"bc04-live-1": self.public_key},
            expected_audience="acquisition-gateway/local-test",
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(verified["expected_initial_target_digest"], digest)

    def test_expiry_is_capped_by_lease_and_hard_deadline(self):
        capability = self._issue(execution_deadline=self.now + timedelta(seconds=40))
        self.assertEqual(
            capability["expires_at"], (self.now + timedelta(seconds=40)).isoformat()
        )
        with self.assertRaisesRegex(CoordinationError, "expired"):
            verify_execution_capability(
                capability,
                trusted_public_keys={"bc04-live-1": self.public_key},
                expected_audience="acquisition-gateway/local-test",
                now=self.now + timedelta(seconds=41),
            )

    def test_forged_expired_wrong_audience_and_unknown_key_capabilities_fail(self):
        original = self._issue()
        forged = dict(original, policy_id="other-policy")
        cases = (
            (
                forged,
                {"bc04-live-1": self.public_key},
                "acquisition-gateway/local-test",
                self.now,
            ),
            (original, {"bc04-live-1": self.public_key}, "other-audience", self.now),
            (original, {}, "acquisition-gateway/local-test", self.now),
            (
                original,
                {"bc04-live-1": self.public_key},
                "acquisition-gateway/local-test",
                self.now + timedelta(minutes=3),
            ),
        )
        for capability, keys, audience, now in cases:
            with (
                self.subTest(audience=audience, now=now),
                self.assertRaises(CoordinationError),
            ):
                verify_execution_capability(
                    capability,
                    trusted_public_keys=keys,
                    expected_audience=audience,
                    now=now,
                )

    def test_expired_attempt_cannot_receive_a_capability(self):
        expired = AttemptLease(
            tenant_id=self.lease.tenant_id,
            run_id=self.lease.run_id,
            attempt_id=self.lease.attempt_id,
            generation=self.lease.generation,
            lease_owner=self.lease.lease_owner,
            lease_token=self.lease.lease_token,
            lease_until=self.now,
            attempt_deadline=self.now + timedelta(seconds=30),
        )
        with self.assertRaisesRegex(CoordinationError, "already expired"):
            self.signer.issue(
                expired,
                source_id="controlled-http",
                source_revision_id="controlled-v1",
                policy_id="controlled-get",
                policy_version="1.0.0",
                policy_digest="sha256:" + "a" * 64,
                release_id="release-controlled-1",
                build_id="sha256:" + "b" * 64,
                build_manifest_digest="sha256:" + "b" * 64,
                runtime_profile_id="scrapy-live-http-v1",
                audience="acquisition-gateway/local-test",
                execution_deadline=self.now + timedelta(seconds=30),
                now=self.now,
            )

    def test_cross_language_conformance_vector_verifies(self):
        path = (
            Path(__file__).parents[3]
            / "contracts/execution-capability/draft/v1/examples/conformance-vector.json"
        )
        vector = json.loads(path.read_text())
        public_key = base64.urlsafe_b64decode(vector["public_key"] + "==")
        verified = verify_execution_capability(
            vector["capability"],
            trusted_public_keys={"vector-key-1": public_key},
            expected_audience="gateway/test",
            now=datetime(2026, 11, 25, 16, 0, 30, tzinfo=UTC),
        )
        self.assertEqual(verified["capability_id"], "conformance-vector-1")


if __name__ == "__main__":
    unittest.main()
