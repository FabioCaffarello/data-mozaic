from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from acquisition_application.result_bundle import (
    FixtureImportError,
    verify_result_bundle,
)
from execution_coordination.adapters.scrapy_container import request_identities
from execution_coordination.domain import ExecutionBinding, ExecutionReport

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contracts/execution-report/draft/v0/ExecutionReport.schema.json"


class FixtureResultBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.fixture_root = root / "fixtures"
        self.fixture_root.mkdir()
        self.run_id = "11111111-1111-4111-8111-111111111111"
        self.attempt_id = "22222222-2222-4222-8222-222222222222"
        self.result_dir = root / "results" / self.run_id / self.attempt_id
        self.result_dir.mkdir(parents=True)
        self.body = b'<article class="product" data-id="synthetic-1"><h2 class="title">Fixture item</h2><span class="price">3.25</span></article>'
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.url = "https://html.fixture.test/catalog/"
        self.evidence_ref = "e-fixture-1"
        (self.fixture_root / "page.html").write_bytes(self.body)
        (self.fixture_root / "index.json").write_text(
            json.dumps(
                {
                    "complete": True,
                    "source": {
                        "source_id": "synthetic-html-catalog",
                        "revision": "fixture-1",
                    },
                    "pages": [
                        {
                            "url": self.url,
                            "evidence_ref": self.evidence_ref,
                            "sha256": self.digest,
                            "size_bytes": len(self.body),
                            "body_file": "page.html",
                            "content_type": "text/html",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.binding = ExecutionBinding(
            source_id="synthetic-html-catalog",
            source_revision="fixture-1",
            bot_id="bot-" + "a" * 64,
            build_id="b" * 64,
            bot_build_manifest_digest="c" * 64,
            assembly_id="d" * 64,
            catalog_digest="e" * 64,
            oci_image="registry.invalid/scrapy@sha256:" + "f" * 64,
            oci_digest="sha256:" + "f" * 64,
            local_image_id="sha256:" + "1" * 64,
            fixture_bundle_id="fixture-bundle-v1",
            fixture_bundle_digest="2" * 64,
        )
        self.record = {
            "id": "synthetic-1",
            "title": "Fixture item",
            "price": "3.25",
            "_provenance": {
                "source_id": self.binding.source_id,
                "source_revision": self.binding.source_revision,
                "requested_url": self.url,
                "effective_url": self.url,
                "source_url": self.url,
                "evidence_ref": self.evidence_ref,
                "raw_sha256": self.digest,
                "operation_id": "extract",
                "page_index": 0,
                "record_index": 0,
            },
        }
        self.records_bytes = (
            json.dumps(self.record, sort_keys=True, separators=(",", ":")).encode()
            + b"\n"
        )
        self.report = ExecutionReport(
            contract_version="execution-report/v0",
            request_id="request-1",
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            execution_id="33333333-3333-4333-8333-333333333333",
            technical_status="succeeded",
            binding=self.binding,
            duration_ms=12,
            records_count=1,
            output_bytes=0,
            output_sha256=hashlib.sha256(self.records_bytes).hexdigest(),
            output_ref=str(self.result_dir),
        )
        self.catalog_entry = SimpleNamespace(
            input_fixtures=[
                {
                    "url": self.url,
                    "evidence_ref": self.evidence_ref,
                    "sha256": self.digest,
                }
            ]
        )
        self.expected = Path(self.temporary.name) / "expected.jsonl"
        self.expected.write_bytes(self.records_bytes)
        self.plan = {
            "profile": "scrapy-fixture-v1",
            "source": {
                "source_id": self.binding.source_id,
                "revision": self.binding.source_revision,
                "origin": "https://html.fixture.test",
                "path_prefixes": ["/catalog/"],
            },
            "budgets": {
                "max_records": 10,
                "max_output_bytes": 1_000_000,
                "max_pages": 1,
            },
            "fetch": {"inputs": {"url": self.url}},
            "select": {
                "operator_type": "select.html_records",
                "inputs": {
                    "selector": "article.product",
                    "cardinality": "many",
                    "max_matches": 10,
                },
            },
            "extract": {
                "operation_id": "extract",
                "inputs": {
                    "fields": {
                        "id": {
                            "source": "attribute",
                            "attribute": "data-id",
                            "type": "string",
                            "required": True,
                            "normalize": ["trim"],
                        },
                        "title": {
                            "source": "text",
                            "selector": ".title",
                            "type": "string",
                            "required": True,
                            "normalize": ["trim"],
                        },
                        "price": {
                            "source": "text",
                            "selector": ".price",
                            "type": "decimal",
                            "required": True,
                            "normalize": ["trim"],
                        },
                    }
                },
            },
        }
        self._write_bundle()

    def _write_bundle(self) -> None:
        identities = request_identities(self.binding)
        runtime_report = json.dumps(
            {
                "state": "passed",
                "records_file": "records.jsonl",
                "records_sha256": hashlib.sha256(self.records_bytes).hexdigest(),
                "records": 1,
            }
        ).encode()
        raw_references = json.dumps(
            [
                {
                    "url": self.url,
                    "evidence_ref": self.evidence_ref,
                    "sha256": self.digest,
                }
            ]
        ).encode()
        outer = {
            "contract_version": "execution-report/v0",
            "request_id": self.report.request_id,
            "run_id": self.report.run_id,
            "attempt_id": self.report.attempt_id,
            "execution_id": self.report.execution_id,
            "identities": identities,
            "technical_status": "succeeded",
            "duration_ms": 12,
            "counts": {
                "records": 1,
                "output_bytes": len(self.records_bytes)
                + len(runtime_report)
                + len(raw_references),
            },
            "diagnostics": [],
            "outputs": {
                "records_sha256": hashlib.sha256(self.records_bytes).hexdigest(),
                "temporary_ref": "worker-output-identity",
            },
        }
        outer_bytes = json.dumps(outer).encode()
        (self.result_dir / "execution-report.json").write_bytes(outer_bytes)
        self.report = replace(
            self.report,
            output_bytes=len(self.records_bytes)
            + len(runtime_report)
            + len(raw_references)
            + len(outer_bytes),
        )
        (self.result_dir / "records.jsonl").write_bytes(self.records_bytes)
        (self.result_dir / "report.json").write_bytes(runtime_report)
        (self.result_dir / "raw-references.json").write_bytes(raw_references)

    def _verify(self):
        return verify_result_bundle(
            self.result_dir,
            self.report,
            fixture_root=self.fixture_root,
            catalog_entry=self.catalog_entry,
            compiled_plan=self.plan,
            execution_report_schema_path=SCHEMA,
        )

    def test_accepts_trusted_composite_result_and_fixture_bytes(self):
        bundle = self._verify()
        self.assertEqual((self.record,), bundle.records)
        self.assertEqual((self.body,), tuple(page.body for page in bundle.pages))
        self.assertEqual(
            hashlib.sha256(self.records_bytes).hexdigest(), bundle.records_digest
        )

    def test_rejects_duplicate_properties_and_invalid_utf8_in_records(self):
        (self.result_dir / "records.jsonl").write_bytes(b'{"id":"a","id":"b"}\n')
        with self.assertRaises(FixtureImportError):
            self._verify()
        self._write_bundle()
        (self.result_dir / "records.jsonl").write_bytes(b'{"id":"\xff"}\n')
        with self.assertRaises(FixtureImportError):
            self._verify()
        (self.result_dir / "records.jsonl").write_bytes(b'{"value":NaN}\n')
        with self.assertRaises(FixtureImportError):
            self._verify()
        (self.result_dir / "records.jsonl").write_bytes(b'{"value":9007199254740992}\n')
        with self.assertRaises(FixtureImportError):
            self._verify()

    def test_rejects_unexpected_files_and_symlinks(self):
        (self.result_dir / "extra.json").write_text("{}")
        with self.assertRaises(FixtureImportError):
            self._verify()
        (self.result_dir / "extra.json").unlink()
        (self.result_dir / "escape").symlink_to(self.expected)
        with self.assertRaises(FixtureImportError):
            self._verify()

    def test_rejects_changed_digest_and_untrusted_raw_reference(self):
        report = json.loads((self.result_dir / "execution-report.json").read_text())
        report["outputs"]["records_sha256"] = "0" * 64
        (self.result_dir / "execution-report.json").write_text(json.dumps(report))
        with self.assertRaises(FixtureImportError):
            self._verify()
        self._write_bundle()
        (self.result_dir / "raw-references.json").write_text(
            json.dumps(
                [
                    {
                        "url": "https://unexpected.invalid/",
                        "evidence_ref": "x",
                        "sha256": "0" * 64,
                    }
                ]
            )
        )
        with self.assertRaises(FixtureImportError):
            self._verify()

    def test_rejects_duplicate_worker_report_properties(self):
        (self.result_dir / "execution-report.json").write_bytes(
            b'{"contract_version":"execution-report/v0",'
            b'"contract_version":"execution-report/v0"}'
        )
        with self.assertRaises(FixtureImportError):
            self._verify()

    def test_rejects_worker_output_byte_count_mismatch(self):
        report = json.loads((self.result_dir / "execution-report.json").read_text())
        report["counts"]["output_bytes"] += 1
        (self.result_dir / "execution-report.json").write_text(json.dumps(report))
        with self.assertRaises(FixtureImportError):
            self._verify()

    def test_rejects_fixture_path_traversal_or_digest_mismatch(self):
        index_path = self.fixture_root / "index.json"
        index = json.loads(index_path.read_text())
        index["pages"][0]["body_file"] = "../escape"
        index_path.write_text(json.dumps(index))
        with self.assertRaises(FixtureImportError):
            self._verify()
        index["pages"][0]["body_file"] = "page.html"
        index["pages"][0]["sha256"] = "0" * 64
        index_path.write_text(json.dumps(index))
        with self.assertRaises(FixtureImportError):
            self._verify()

    def test_rejects_symlinked_fixture_root(self):
        alias = Path(self.temporary.name) / "fixture-alias"
        alias.symlink_to(self.fixture_root, target_is_directory=True)
        self.fixture_root = alias
        with self.assertRaises(FixtureImportError):
            self._verify()

    def test_production_validation_does_not_consume_test_oracle_files(self):
        self.expected.write_text("{}\n")
        bundle = self._verify()
        self.assertEqual((self.record,), bundle.records)


if __name__ == "__main__":
    unittest.main()
