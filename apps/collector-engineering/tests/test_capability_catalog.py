from __future__ import annotations

import json
import unittest
from pathlib import Path

from collector_engineering.backend_scrapy_live import RAW_V4_OPERATORS
from collector_engineering.capabilities import capability_catalog

ROOT = Path(__file__).resolve().parents[3]


class CapabilityCatalogTests(unittest.TestCase):
    def test_surface_is_derived_from_backend_profiles_and_raw_schema(self):
        catalog = capability_catalog()
        declared = {}
        for path in sorted(
            (ROOT / "apps/collector-engineering/collector_engineering/profiles").glob(
                "*.json"
            )
        ):
            profile = json.loads(path.read_text())
            declared[profile["backend_id"]] = sorted(profile["capabilities"])
        self.assertEqual(
            declared,
            {item["backend_id"]: item["capabilities"] for item in catalog["backends"]},
        )
        self.assertEqual("bc15-authoring-capabilities/v1", catalog["catalog_version"])
        self.assertEqual(
            {
                "capture-playbook-draft/v1",
                "capture-playbook-draft/v2",
                "capture-playbook-draft/v3",
                "capture-playbook-draft/v4",
            },
            {item["version"] for item in catalog["playbook_versions"]},
        )
        self.assertEqual(
            1, catalog["parameterized_profile"]["max_scalar_query_bindings"]
        )
        self.assertFalse(catalog["parameterized_profile"]["supports_pagination"])
        self.assertEqual(
            2_000_000, catalog["raw_artifact_profile"]["max_artifact_bytes"]
        )
        self.assertFalse(catalog["raw_artifact_profile"]["requires_input_contract"])
        self.assertIn(
            "output.artifact/v1",
            catalog["raw_artifact_profile"]["supported_operations"],
        )

    def test_raw_profile_operations_match_the_live_compiler_graph(self):
        public_schema = json.loads(
            (
                ROOT / "contracts/capture-playbook/draft/v4/CapturePlaybook.schema.json"
            ).read_text()
        )
        packaged_schema = json.loads(
            (
                ROOT
                / "apps/collector-engineering/collector_engineering/schemas/CapturePlaybook.v4.schema.json"
            ).read_text()
        )
        self.assertEqual(public_schema, packaged_schema)
        schema = packaged_schema
        declared = sorted(
            item["$ref"].rsplit("/", 1)[-1]
            for item in schema["properties"]["operations"]["items"]["oneOf"]
        )
        self.assertEqual(
            sorted(RAW_V4_OPERATORS),
            capability_catalog()["raw_artifact_profile"]["supported_operations"],
        )
        expected_refs = {
            "fetch" if item == "http.fetch/v1" else "output_artifact"
            for item in RAW_V4_OPERATORS
        }
        self.assertTrue(expected_refs <= set(declared))


if __name__ == "__main__":
    unittest.main()
