from __future__ import annotations

import json
import unittest
from pathlib import Path

from execution_coordination.adapters.publication_grant import (
    Ed25519PublicationGrantSigner,
)
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


class DurableGrantContractTests(unittest.TestCase):
    def test_draft_contract_fixtures_are_independently_schema_checked(self):
        schema = json.loads(CONTRACT.read_text())
        validator = Draft202012Validator(schema)
        self.assertEqual(list(validator.iter_errors(json.loads(VALID.read_text()))), [])
        self.assertTrue(list(validator.iter_errors(json.loads(INVALID.read_text()))))

    def test_signer_matches_independent_contract_conformance_vector(self):
        secret = bytes(range(32))
        signer = Ed25519PublicationGrantSigner(secret, key_id="test-vector-v1")
        grant = signer.issue(
            {
                "tenant_id": "tenant-fixture",
                "run_id": "11111111-1111-4111-8111-111111111111",
                "attempt_id": "22222222-2222-4222-8222-222222222222",
                "fencing_generation": 1,
                "output_set_id": "set-fixture-001",
                "manifest_digest": "sha256:" + "5" * 64,
                "reservation_id": "res-" + "a" * 64,
                "operation_id": "reserve-fixture-001",
                "issued_at": "2026-01-01T00:00:00+00:00",
            }
        )
        fixture = json.loads(VALID.read_text())
        self.assertEqual(grant.fields(), fixture)


if __name__ == "__main__":
    unittest.main()
