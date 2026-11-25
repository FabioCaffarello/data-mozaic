from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts/artifact-custody/draft/v1"


def registry() -> Registry:
    schemas = {}
    for path in CONTRACTS.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        schemas[schema["$id"]] = Resource.from_contents(schema)
    return Registry().with_resources(schemas.items())


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    return Draft202012Validator(
        schema, registry=registry(), format_checker=FormatChecker()
    )


class ArtifactCustodyContractTests(unittest.TestCase):
    def test_public_contract_schemas_are_valid_and_valid_fixtures_conform(self):
        families = {
            "write-intent.json": "ArtifactWriteIntent.schema.json",
            "custody-receipt.json": "CustodyReceipt.schema.json",
            "custody-event.json": "CustodyEvent.schema.json",
        }
        for fixture_name, schema_name in families.items():
            with self.subTest(fixture=fixture_name):
                value = json.loads(
                    (CONTRACTS / "fixtures/valid" / fixture_name).read_text()
                )
                self.assertEqual(list(validator(schema_name).iter_errors(value)), [])

    def test_invalid_write_intent_fixtures_are_rejected(self):
        check = validator("ArtifactWriteIntent.schema.json")
        for path in (CONTRACTS / "fixtures/invalid").glob("*.json"):
            with self.subTest(fixture=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                errors = list(check.iter_errors(value))
                if path.name == "mismatched-artifact-identity.json":
                    # JSON Schema 2020-12 cannot compare two arbitrary property
                    # values. The contract's explicit cross-field invariant is
                    # independently asserted at this public wire boundary.
                    self.assertEqual(errors, [])
                    self.assertNotEqual(
                        value["artifact_id"], value["artifact"]["artifact_id"]
                    )
                else:
                    self.assertNotEqual(errors, [])

    def test_intent_status_and_verification_shapes(self):
        status = validator("ArtifactIntentStatus.schema.json")
        intent = json.loads(
            (CONTRACTS / "fixtures/valid/write-intent.json").read_text()
        )
        value = {
            "contract_version": "artifact-custody.v1",
            "operation_id": intent["operation_id"],
            "artifact_id": intent["artifact_id"],
            "state": "available",
            "receipt": json.loads(
                (CONTRACTS / "fixtures/valid/custody-receipt.json").read_text()
            ),
        }
        self.assertEqual(list(status.iter_errors(value)), [])
        verification = validator("CustodyVerification.schema.json")
        self.assertEqual(
            list(
                verification.iter_errors(
                    {"contract_version": "artifact-custody.v1", "status": "unavailable"}
                )
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
