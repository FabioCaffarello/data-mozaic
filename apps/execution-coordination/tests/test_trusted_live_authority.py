from __future__ import annotations

import base64
import hashlib
import json
import unittest
from datetime import UTC, datetime, timedelta

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.trusted_live_authority import (
    TrustedLiveAuthority,
)
from execution_coordination.domain import CoordinationError


class TrustedLiveAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 25, 15, 0, tzinfo=UTC)
        self.private_key = Ed25519PrivateKey.from_private_bytes(b"k" * 32)
        self.public_key = self.private_key.public_key().public_bytes_raw()
        self.source = {
            "source_id": "controlled-http",
            "revision_id": "controlled-v1",
            "status": "eligible",
        }
        self.source["configuration_digest"] = self._document_digest(
            self.source, "configuration_digest"
        )
        self.policy = {
            "policy_id": "controlled-get",
            "policy_version": "1.0.0",
            "source_id": "controlled-http",
            "source_revision_id": "controlled-v1",
            "status": "eligible",
        }
        self.policy["policy_digest"] = self._document_digest(
            self.policy, "policy_digest"
        )
        self.release = {
            "contract_version": "bot-release/v2",
            "release_id": "build-release-1",
            "status": "approved",
            "source_id": "controlled-http",
            "source_revision_id": "controlled-v1",
            "policy_id": "controlled-get",
            "policy_version": "1.0.0",
            "build_id": "sha256:" + "a" * 64,
            "build_manifest_digest": "sha256:" + "b" * 64,
            "backend_id": "scrapy-live-http",
            "runtime_profile_id": "scrapy-live-http-v1",
            "runtime_image_digest": "sha256:" + "c" * 64,
            "payload_contract": {
                "contract_id": "controlled.record",
                "version": "1.0.0",
                "digest": "sha256:" + "d" * 64,
            },
        }
        self.release["release_digest"] = self._document_digest(
            self.release, "release_digest"
        )
        self.snapshot = {
            "contract_version": "live-authority-snapshot/v1",
            "authority_id": "controlled-authority-test",
            "generation": 7,
            "issued_at": (self.now - timedelta(minutes=1)).isoformat(),
            "expires_at": (self.now + timedelta(minutes=10)).isoformat(),
            "source_revisions": [self.source],
            "policies": [self.policy],
            "releases": [self.release],
        }
        self.snapshot["snapshot_digest"] = (
            "sha256:" + hashlib.sha256(rfc8785.dumps(self.snapshot)).hexdigest()
        )

    def _encoded(self, snapshot=None) -> bytes:
        payload = self.snapshot if snapshot is None else snapshot
        signed = rfc8785.dumps(payload)
        envelope = {
            "snapshot": payload,
            "key_id": "operator-key-1",
            "signature": base64.urlsafe_b64encode(self.private_key.sign(signed))
            .decode("ascii")
            .rstrip("="),
        }
        return json.dumps(envelope, separators=(",", ":")).encode()

    @staticmethod
    def _document_digest(document, field):
        unsigned = {key: value for key, value in document.items() if key != field}
        return "sha256:" + hashlib.sha256(rfc8785.dumps(unsigned)).hexdigest()

    def _load(self, data=None, *, minimum_generation=7, now=None):
        return TrustedLiveAuthority(
            self._encoded() if data is None else data,
            trusted_public_keys={"operator-key-1": self.public_key},
            minimum_generation=minimum_generation,
            now=self.now if now is None else now,
        )

    def test_signed_snapshot_resolves_exact_approved_bindings(self):
        binding = self._load().resolve(
            source_id="controlled-http",
            source_revision_id="controlled-v1",
            policy_id="controlled-get",
            policy_version="1.0.0",
            release_id="build-release-1",
            build_id="sha256:" + "a" * 64,
            build_manifest_digest="sha256:" + "b" * 64,
            runtime_image_digest="sha256:" + "c" * 64,
        )
        self.assertEqual(binding.snapshot_generation, 7)
        self.assertEqual(binding.release["release_id"], "build-release-1")
        self.assertEqual(
            binding.release["payload_contract"]["contract_id"], "controlled.record"
        )
        self.assertTrue(binding.snapshot_digest.startswith("sha256:"))

    def test_v3_release_preserves_the_exact_input_contract_binding(self):
        snapshot = json.loads(json.dumps(self.snapshot))
        release = snapshot["releases"][0]
        release["contract_version"] = "bot-release/v3"
        release["input_contract"] = {
            "contract_id": "controlled.input",
            "version": "1.0.0",
            "digest": "sha256:" + "e" * 64,
        }
        release["input_request"] = {
            "method": "GET",
            "url": "https://example.test/api/v1/items",
            "input_query": {"parameter": "limit", "query_name": "limit"},
        }
        release["release_digest"] = self._document_digest(release, "release_digest")
        snapshot.pop("snapshot_digest")
        snapshot["snapshot_digest"] = self._document_digest(snapshot, "snapshot_digest")
        binding = self._load(self._encoded(snapshot)).resolve(
            source_id="controlled-http",
            source_revision_id="controlled-v1",
            policy_id="controlled-get",
            policy_version="1.0.0",
            release_id="build-release-1",
            build_id="sha256:" + "a" * 64,
            build_manifest_digest="sha256:" + "b" * 64,
            runtime_image_digest="sha256:" + "c" * 64,
        )
        self.assertEqual(
            binding.release["input_contract"]["digest"], "sha256:" + "e" * 64
        )

    def test_historical_release_v1_is_not_admitted_as_contract_bound(self):
        historical = json.loads(json.dumps(self.snapshot))
        release = historical["releases"][0]
        release["contract_version"] = "bot-release/v1"
        release.pop("payload_contract")
        release["release_digest"] = self._document_digest(release, "release_digest")
        historical.pop("snapshot_digest")
        historical["snapshot_digest"] = self._document_digest(
            historical, "snapshot_digest"
        )
        with self.assertRaisesRegex(CoordinationError, "contract version"):
            self._load(self._encoded(historical)).resolve(
                source_id="controlled-http",
                source_revision_id="controlled-v1",
                policy_id="controlled-get",
                policy_version="1.0.0",
                release_id="build-release-1",
                build_id="sha256:" + "a" * 64,
                build_manifest_digest="sha256:" + "b" * 64,
                runtime_image_digest="sha256:" + "c" * 64,
            )

    def test_unsigned_or_tampered_approval_flag_is_not_authorization(self):
        tampered = json.loads(self._encoded())
        tampered["snapshot"]["releases"][0]["release_id"] = "forged-release"
        data = json.dumps(tampered).encode()
        with self.assertRaisesRegex(CoordinationError, "signature"):
            self._load(data)

    def test_unknown_and_revoked_authority_entries_fail_closed(self):
        authority = self._load()
        with self.assertRaisesRegex(CoordinationError, "unknown"):
            authority.resolve(
                source_id="unknown",
                source_revision_id="controlled-v1",
                policy_id="controlled-get",
                policy_version="1.0.0",
                release_id="build-release-1",
                build_id="sha256:" + "a" * 64,
                build_manifest_digest="sha256:" + "b" * 64,
                runtime_image_digest="sha256:" + "c" * 64,
            )
        revoked = json.loads(json.dumps(self.snapshot))
        revoked["releases"][0]["status"] = "revoked"
        revoked.pop("snapshot_digest")
        revoked["snapshot_digest"] = (
            "sha256:" + hashlib.sha256(rfc8785.dumps(revoked)).hexdigest()
        )
        with self.assertRaisesRegex(CoordinationError, "not currently approved"):
            self._load(self._encoded(revoked)).resolve(
                source_id="controlled-http",
                source_revision_id="controlled-v1",
                policy_id="controlled-get",
                policy_version="1.0.0",
                release_id="build-release-1",
                build_id="sha256:" + "a" * 64,
                build_manifest_digest="sha256:" + "b" * 64,
                runtime_image_digest="sha256:" + "c" * 64,
            )

    def test_expired_or_rolled_back_snapshot_is_rejected(self):
        with self.assertRaisesRegex(CoordinationError, "generation"):
            self._load(minimum_generation=8)
        with self.assertRaisesRegex(CoordinationError, "currently valid"):
            self._load(now=self.now + timedelta(minutes=11))

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaisesRegex(CoordinationError, "duplicate"):
            self._load(b'{"snapshot":{},"snapshot":{},"key_id":"x","signature":"x"}')


if __name__ == "__main__":
    unittest.main()
