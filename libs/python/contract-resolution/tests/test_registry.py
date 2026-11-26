from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mozaic_contract_resolution import (
    ContractReference,
    ContractResolutionError,
    ImmutableContractRegistry,
)


class ImmutableContractRegistryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[4] / "contracts/payload/draft/v1"
        self.registry = ImmutableContractRegistry(self.root)

    def test_resolves_exact_installed_revision_and_validates(self):
        reference = self.registry.installed_reference(
            "controlled-live-json.record", "1.0.0"
        )
        resolved = self.registry.resolve(reference)
        self.assertEqual(reference, resolved.reference)
        resolved.validate(
            {
                "id": "controlled-1",
                "quantity": 2,
                "unit_price": "1.25",
                "enabled": True,
                "_provenance": {
                    "source_id": "controlled-http",
                    "source_revision": "controlled-v1",
                    "requested_url": "https://api.fixture.test/v1/items",
                    "effective_url": "https://api.fixture.test/v1/items",
                    "source_url": "https://api.fixture.test/v1/items",
                    "evidence_ref": "fetch-1",
                    "raw_sha256": "a" * 64,
                    "operation_id": "operation-1",
                    "page_index": 0,
                    "record_index": 0,
                },
            }
        )

    def test_rejects_unknown_revision_and_digest_divergence(self):
        reference = self.registry.installed_reference(
            "controlled-live-json.record", "1.0.0"
        )
        with self.assertRaisesRegex(ContractResolutionError, "unresolved"):
            self.registry.resolve(
                ContractReference("missing.record", "1.0.0", reference.digest)
            )
        with self.assertRaisesRegex(ContractResolutionError, "digest"):
            self.registry.resolve(
                ContractReference(
                    reference.contract_id, reference.version, "sha256:" + "0" * 64
                )
            )

    def test_schema_content_is_verified_on_every_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            schema = root / "schema.json"
            raw = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string"}'
            schema.write_bytes(raw)
            manifest = {
                "contract_version": "payload-contract-registry/v1",
                "revisions": [
                    {
                        "contract_id": "sample.record",
                        "version": "1.0.0",
                        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "schema_path": schema.name,
                        "dialect": "https://json-schema.org/draft/2020-12/schema",
                    }
                ],
            }
            (root / "registry.json").write_text(json.dumps(manifest))
            registry = ImmutableContractRegistry(root)
            schema.write_text(
                '{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"number"}'
            )
            with self.assertRaisesRegex(ContractResolutionError, "digest"):
                registry.resolve(registry.installed_reference("sample.record", "1.0.0"))

    def test_new_revision_does_not_change_historical_resolution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            older = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string"}'
            newer = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"integer"}'
            (root / "v1.json").write_bytes(older)
            (root / "v2.json").write_bytes(newer)
            revisions = []
            for version, path, raw in (
                ("1.0.0", "v1.json", older),
                ("1.1.0", "v2.json", newer),
            ):
                revisions.append(
                    {
                        "contract_id": "sample.record",
                        "version": version,
                        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "schema_path": path,
                        "dialect": "https://json-schema.org/draft/2020-12/schema",
                    }
                )
            (root / "registry.json").write_text(
                json.dumps(
                    {
                        "contract_version": "payload-contract-registry/v1",
                        "revisions": revisions,
                    }
                )
            )
            registry = ImmutableContractRegistry(root)
            old = registry.resolve(
                registry.installed_reference("sample.record", "1.0.0")
            )
            old.validate("historical")
            with self.assertRaises(ContractResolutionError):
                old.validate(3)
            registry.resolve(
                registry.installed_reference("sample.record", "1.1.0")
            ).validate(3)

    def test_rejects_same_identity_version_with_conflicting_install_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string"}'
            second = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"number"}'
            (root / "one.json").write_bytes(first)
            (root / "two.json").write_bytes(second)
            entries = [
                {
                    "contract_id": "sample.record",
                    "version": "1.0.0",
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                    "schema_path": path,
                    "dialect": "https://json-schema.org/draft/2020-12/schema",
                }
                for path, content in (("one.json", first), ("two.json", second))
            ]
            (root / "registry.json").write_text(
                json.dumps(
                    {
                        "contract_version": "payload-contract-registry/v1",
                        "revisions": entries,
                    }
                )
            )
            with self.assertRaisesRegex(ContractResolutionError, "conflicting"):
                ImmutableContractRegistry(root)

    def test_rejects_invalid_schema_and_unsupported_dialect(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"unsupported"}'
            (root / "schema.json").write_bytes(raw)
            manifest = {
                "contract_version": "payload-contract-registry/v1",
                "revisions": [
                    {
                        "contract_id": "sample.record",
                        "version": "1.0.0",
                        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "schema_path": "schema.json",
                        "dialect": "https://json-schema.org/draft/2020-12/schema",
                    }
                ],
            }
            (root / "registry.json").write_text(json.dumps(manifest))
            registry = ImmutableContractRegistry(root)
            with self.assertRaisesRegex(ContractResolutionError, "schema is invalid"):
                registry.resolve(registry.installed_reference("sample.record", "1.0.0"))

    def test_rejects_external_schema_reference_and_duplicate_manifest_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","$ref":"https://example.invalid/schema"}'
            (root / "schema.json").write_bytes(raw)
            manifest = {
                "contract_version": "payload-contract-registry/v1",
                "revisions": [
                    {
                        "contract_id": "sample.record",
                        "version": "1.0.0",
                        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "schema_path": "schema.json",
                        "dialect": "https://json-schema.org/draft/2020-12/schema",
                    }
                ],
            }
            (root / "registry.json").write_text(json.dumps(manifest))
            registry = ImmutableContractRegistry(root)
            with self.assertRaisesRegex(ContractResolutionError, "references"):
                registry.resolve(registry.installed_reference("sample.record", "1.0.0"))
            (root / "registry.json").write_text(
                '{"contract_version":"payload-contract-registry/v1","contract_version":"payload-contract-registry/v1","revisions":[]}'
            )
            with self.assertRaisesRegex(ContractResolutionError, "duplicate"):
                ImmutableContractRegistry(root)

    def test_rejects_dynamic_schema_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = b'{"$schema":"https://json-schema.org/draft/2020-12/schema","$dynamicRef":"https://example.invalid/schema"}'
            (root / "schema.json").write_bytes(raw)
            manifest = {
                "contract_version": "payload-contract-registry/v1",
                "revisions": [
                    {
                        "contract_id": "sample.record",
                        "version": "1.0.0",
                        "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "schema_path": "schema.json",
                        "dialect": "https://json-schema.org/draft/2020-12/schema",
                    }
                ],
            }
            (root / "registry.json").write_text(json.dumps(manifest))
            registry = ImmutableContractRegistry(root)
            with self.assertRaisesRegex(ContractResolutionError, "references"):
                registry.resolve(registry.installed_reference("sample.record", "1.0.0"))


if __name__ == "__main__":
    unittest.main()
