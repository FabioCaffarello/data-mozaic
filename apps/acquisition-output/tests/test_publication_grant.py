from __future__ import annotations

import json
import unittest
from pathlib import Path

from acquisition_output.adapters.publication_grant import (
    Ed25519PublicationGrantVerifier,
)
from acquisition_output.domain import OutputError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = (
    ROOT / "contracts/execution-coordination/draft/v1/PublicationGrant.schema.json"
)
VALID = (
    ROOT
    / "contracts/execution-coordination/draft/v1/fixtures/valid/publication-grant.json"
)
INVALID = (
    ROOT
    / "contracts/execution-coordination/draft/v1/fixtures/invalid/other-recipient.json"
)


class PublicationGrantContractTests(unittest.TestCase):
    def test_contract_fixtures_are_schema_checked(self):
        validator = Draft202012Validator(json.loads(CONTRACT.read_text()))
        self.assertEqual(list(validator.iter_errors(json.loads(VALID.read_text()))), [])
        self.assertTrue(list(validator.iter_errors(json.loads(INVALID.read_text()))))

    def test_public_key_verifier_accepts_vector_and_rejects_tampering(self):
        public_key = (
            Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        verifier = Ed25519PublicationGrantVerifier({"test-vector-v1": public_key})
        self.assertFalse(hasattr(verifier, "issue"))
        fixture = json.loads(VALID.read_text())
        authorization = verifier.verify(fixture)
        self.assertEqual(authorization.reservation_id, fixture["reservation_id"])
        for field, value in (
            ("tenant_id", "another-tenant"),
            ("run_id", "33333333-3333-4333-8333-333333333333"),
            ("attempt_id", "44444444-4444-4444-8444-444444444444"),
            ("fencing_generation", 2),
            ("output_set_id", "another-set"),
            ("manifest_digest", "sha256:" + "6" * 64),
            ("reservation_id", "res-" + "b" * 64),
            ("operation_id", "another-operation"),
            ("recipient", "urn:data-mozaic:bc04:execution-coordination"),
        ):
            changed = dict(fixture)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(OutputError):
                verifier.verify(changed)
        changed = dict(fixture)
        changed["signature"] = "ed25519:A" + fixture["signature"][9:]
        with self.assertRaises(OutputError):
            verifier.verify(changed)
        with self.assertRaises(OutputError):
            verifier.verify(json.loads(INVALID.read_text()))

    def test_verifier_rejects_unknown_key_and_algorithm(self):
        public_key = (
            Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        verifier = Ed25519PublicationGrantVerifier({"test-vector-v1": public_key})
        fixture = json.loads(VALID.read_text())
        fixture["key_id"] = "untrusted-key"
        with self.assertRaises(OutputError):
            verifier.verify(fixture)
        fixture = json.loads(VALID.read_text())
        fixture["algorithm"] = "HMAC-SHA256"
        with self.assertRaises(OutputError):
            verifier.verify(fixture)

    def test_key_rotation_retains_old_verification_keys(self):
        old_seed = bytes(range(32))
        new_seed = bytes(reversed(range(32)))
        old_public = (
            Ed25519PrivateKey.from_private_bytes(old_seed)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        new_public = (
            Ed25519PrivateKey.from_private_bytes(new_seed)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        verifier = Ed25519PublicationGrantVerifier(
            {"test-vector-v1": old_public, "test-vector-v2": new_public}
        )
        authorization = verifier.verify(json.loads(VALID.read_text()))
        self.assertEqual(authorization.reservation_id, "res-" + "a" * 64)


if __name__ == "__main__":
    unittest.main()
