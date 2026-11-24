from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import rfc8785
from acquisition_output.domain import OutputError
from acquisition_output.wire import capture_output_from_json, capture_output_from_wire
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_ROOT = ROOT / "contracts/acquisition-output/draft/v1"


class AcquisitionOutputContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schemas = {
            path.name: json.loads(path.read_text())
            for path in CONTRACT_ROOT.glob("*.schema.json")
        }
        registry = Registry()
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)
            registry = registry.with_resource(
                schema["$id"], Resource.from_contents(schema)
            )
        cls.registry = registry
        cls.validators = {
            name: Draft202012Validator(
                schema, registry=registry, format_checker=FormatChecker()
            )
            for name, schema in cls.schemas.items()
        }

    def test_all_valid_contract_fixtures_validate(self):
        schema_names = {
            "record-output.json": "CaptureOutput.schema.json",
            "artifact-output.json": "CaptureOutput.schema.json",
            "composite-output.json": "CaptureOutput.schema.json",
            "output-set-staging.json": "OutputSetManifest.schema.json",
            "output-set-published.json": "OutputSetManifest.schema.json",
            "output-receipt.json": "OutputReceipt.schema.json",
            "publication-receipt.json": "PublicationReceipt.schema.json",
        }
        fixture_root = CONTRACT_ROOT / "fixtures/valid"
        self.assertEqual(
            {item.name for item in fixture_root.glob("*.json")}, set(schema_names)
        )
        for filename, schema_name in schema_names.items():
            with self.subTest(fixture=filename):
                document = json.loads((fixture_root / filename).read_text())
                errors = list(self.validators[schema_name].iter_errors(document))
                self.assertEqual(errors, [])

    def test_invalid_contract_fixtures_fail_schema_validation(self):
        fixture_root = CONTRACT_ROOT / "fixtures/invalid"
        for path in sorted(fixture_root.glob("*.json")):
            if path.name == "operation-tenant-mismatch.json":
                continue  # schema-valid; the private domain mapping rejects it semantically.
            with self.subTest(fixture=path.name):
                document = json.loads(path.read_text())
                errors = list(
                    self.validators["CaptureOutput.schema.json"].iter_errors(document)
                )
                self.assertTrue(
                    errors, f"fixture unexpectedly passed schema: {path.name}"
                )

    def test_private_domain_mapping_enforces_semantics_and_payload_digest(self):
        valid = json.loads(
            (CONTRACT_ROOT / "fixtures/valid/record-output.json").read_text()
        )
        output = capture_output_from_wire(valid)
        self.assertEqual(output.to_wire(), valid)

        tenant_mismatch = json.loads(
            (
                CONTRACT_ROOT / "fixtures/invalid/operation-tenant-mismatch.json"
            ).read_text()
        )
        self.assertEqual(
            list(
                self.validators["CaptureOutput.schema.json"].iter_errors(
                    tenant_mismatch
                )
            ),
            [],
        )
        with self.assertRaises(OutputError):
            capture_output_from_wire(tenant_mismatch)

        wrong_digest = dict(valid)
        wrong_digest["payload_digest"] = "sha256:" + "0" * 64
        with self.assertRaises(OutputError):
            capture_output_from_wire(wrong_digest)

        unknown_field = {**valid, "database_uri": "not-permitted"}
        with self.assertRaises(OutputError):
            capture_output_from_wire(unknown_field)

        explicit_null = json.loads(json.dumps(valid))
        explicit_null["natural_key"] = None
        with self.assertRaises(OutputError):
            capture_output_from_wire(explicit_null)

        explicit_null_execution = json.loads(json.dumps(valid))
        explicit_null_execution["binding"]["execution"]["run_id"] = None
        with self.assertRaises(OutputError):
            capture_output_from_wire(explicit_null_execution)

    def test_raw_json_boundary_rejects_duplicate_keys_bad_unicode_and_nan(self):
        raw = (CONTRACT_ROOT / "fixtures/valid/record-output.json").read_text()
        duplicate = raw.replace(
            '"output_type": "record"',
            '"output_type": "record", "output_type": "artifact"',
        )
        with self.assertRaises(OutputError):
            capture_output_from_json(duplicate)
        with self.assertRaises(OutputError):
            capture_output_from_json(raw.replace('"price": 12.5', '"price": NaN'))
        with self.assertRaises(OutputError):
            capture_output_from_json(b"\xff")

    def test_independent_rfc8785_fixture_digest_and_set_manifest_digest(self):
        output = json.loads(
            (CONTRACT_ROOT / "fixtures/valid/record-output.json").read_text()
        )
        payload_digest = (
            "sha256:" + hashlib.sha256(rfc8785.dumps(output["payload"])).hexdigest()
        )
        self.assertEqual(output["payload_digest"], payload_digest)
        output_intent = "sha256:" + hashlib.sha256(rfc8785.dumps(output)).hexdigest()

        set_fixture = json.loads(
            (CONTRACT_ROOT / "fixtures/valid/output-set-published.json").read_text()
        )
        self.assertEqual(set_fixture["members"][0]["intent_digest"], output_intent)
        manifest_input = {
            "manifest_version": "output-set-manifest/v1",
            "output_set_id": set_fixture["output_set_id"],
            "binding": set_fixture["binding"],
            "payload_contracts": set_fixture["payload_contracts"],
            "member_count": set_fixture["member_count"],
            "encoded_output_bytes": len(rfc8785.dumps(output)),
            "artifact_reference_count": set_fixture["artifact_reference_count"],
            "custody_reference_count": set_fixture["custody_reference_count"],
            "members": set_fixture["members"],
        }
        expected = "sha256:" + hashlib.sha256(rfc8785.dumps(manifest_input)).hexdigest()
        self.assertEqual(
            set_fixture["encoded_output_bytes"], len(rfc8785.dumps(output))
        )
        self.assertEqual(set_fixture["manifest_digest"], expected)


if __name__ == "__main__":
    unittest.main()
