from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from acquisition_application.live_evidence import (
    _fetch_trusted_evidence,
    _plan_pagination_urls,
    verify_live_result_bundle,
)
from acquisition_application.plan_validation import verify_plan_results
from acquisition_application.result_bundle import (
    FixtureImportError,
    VerifiedFixturePage,
)


class AcquisitionPlanValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = {
            "source_id": "controlled-http",
            "revision": "controlled-v1",
            "origin": "https://api.fixture.test",
            "path_prefixes": ["/v1/"],
        }
        self.url = "https://api.fixture.test/v1/items"
        self.plan = {
            "profile": "scrapy-live-http-v1",
            "source": self.source,
            "fetch": {"inputs": {"url": self.url}},
            "budgets": {"max_records": 10, "max_output_bytes": 100_000, "max_pages": 1},
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
        self.body = b'<article class="product" data-id=" sku-1 "><h2 class="title"> Mug </h2><span class="price">12.50</span></article>'
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.page = VerifiedFixturePage(
            self.url,
            "fetch-1",
            "sha256:" + self.digest,
            "text/html",
            self.body,
        )
        self.record = {
            "id": "sku-1",
            "title": "Mug",
            "price": "12.5",
            "_provenance": {
                "source_id": "controlled-http",
                "source_revision": "controlled-v1",
                "source_url": self.url,
                "requested_url": self.url,
                "effective_url": self.url,
                "evidence_ref": "fetch-1",
                "raw_sha256": self.digest,
                "operation_id": "extract",
                "page_index": 0,
                "record_index": 0,
            },
        }

    def test_result_is_recomputed_from_compiled_plan_and_trusted_body(self):
        verify_plan_results(
            plan=self.plan,
            pages=(self.page,),
            records=(self.record,),
            source_id="controlled-http",
            source_revision="controlled-v1",
        )

    def test_worker_cannot_replace_extracted_value_or_source_binding(self):
        changed = {**self.record, "title": "Forged"}
        with self.assertRaises(FixtureImportError):
            verify_plan_results(
                plan=self.plan,
                pages=(self.page,),
                records=(changed,),
                source_id="controlled-http",
                source_revision="controlled-v1",
            )
        with self.assertRaises(FixtureImportError):
            verify_plan_results(
                plan=self.plan,
                pages=(self.page,),
                records=(self.record,),
                source_id="other-tenant-source",
                source_revision="controlled-v1",
            )

    def test_planned_pagination_is_recomputed_from_plan_and_page_bodies(self):
        first = VerifiedFixturePage(
            self.url,
            "fetch-1",
            "sha256:" + "1" * 64,
            "text/html",
            b'<a class="next" href="/v1/pages/2">Next</a>',
        )
        second_url = "https://api.fixture.test/v1/pages/2"
        second = VerifiedFixturePage(
            second_url,
            "fetch-2",
            "sha256:" + "2" * 64,
            "text/html",
            b'<a class="next" href="/v1/pages/3">Next</a>',
        )
        plan = {
            **self.plan,
            "discover": {"inputs": {"selector": "a.next", "max_links": 1}},
            "pagination": {
                "inputs": {"max_pages": 2, "completion_mode": "bounded_window"}
            },
        }
        _plan_pagination_urls(plan, [first, second], "planned_page_limit")
        with self.assertRaises(FixtureImportError):
            _plan_pagination_urls(plan, [first], "planned_page_limit")

    def test_natural_pagination_requires_evidence_of_natural_end(self):
        first = VerifiedFixturePage(
            self.url,
            "fetch-1",
            "sha256:" + "1" * 64,
            "text/html",
            b'<a class="next" href="/v1/pages/2">Next</a>',
        )
        second_url = "https://api.fixture.test/v1/pages/2"
        second = VerifiedFixturePage(
            second_url,
            "fetch-2",
            "sha256:" + "2" * 64,
            "text/html",
            b"<main>End</main>",
        )
        plan = {
            **self.plan,
            "discover": {"inputs": {"selector": "a.next", "max_links": 1}},
            "pagination": {
                "inputs": {"max_pages": 3, "completion_mode": "natural_end"}
            },
        }
        _plan_pagination_urls(plan, [first, second], "natural_end")
        with self.assertRaises(FixtureImportError):
            _plan_pagination_urls(plan, [first], "natural_end")


class TrustedEvidenceTransferTests(unittest.TestCase):
    body = b"verified controlled evidence"
    fetch_id = "a" * 32

    def _metadata(self, **changes):
        return {
            "contract_version": "fetch-evidence/v1",
            "fetch_id": self.fetch_id,
            "tenant_id": "tenant-a",
            "run_id": "run-a",
            "attempt_id": "attempt-a",
            "generation": 3,
            "source_id": "controlled-http",
            "source_revision_id": "controlled-revision-1",
            "policy_digest": "sha256:" + "1" * 64,
            "release_id": "release-a",
            "build_manifest_digest": "sha256:" + "2" * 64,
            "request_target_digest": "sha256:" + "3" * 64,
            "effective_target_digest": "sha256:" + "4" * 64,
            "request_origin": "https://api.fixture.test",
            "effective_origin": "https://api.fixture.test",
            "request_path_digest": "sha256:" + "5" * 64,
            "effective_path_digest": "sha256:" + "5" * 64,
            "query_parameter_names": [],
            "redirect_count": 0,
            "status": 200,
            "acquired_at": "2026-11-27T20:00:00+00:00",
            "media_type": "application/octet-stream",
            "body_representation": "decoded-http-entity",
            "body_size": len(self.body),
            "body_digest": "sha256:" + hashlib.sha256(self.body).hexdigest(),
            "body_ref": self.fetch_id + ".body",
            **changes,
        }

    def _response(self, metadata):
        encoded = (
            base64.urlsafe_b64encode(
                json.dumps(metadata, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )

        class Response:
            status = 200

            def __init__(self):
                self.headers = {"X-Mozaic-Evidence": encoded}
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                self.reads += 1
                return TrustedEvidenceTransferTests.body if self.reads == 1 else b""

        class Opener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        return Opener()

    def _fetch(self, spool: Path, metadata):
        with mock.patch(
            "acquisition_application.live_evidence.build_opener",
            return_value=self._response(metadata),
        ):
            return _fetch_trusted_evidence(
                endpoint="https://gateway.fixture.test",
                token="test-only-token",
                fetch_id=self.fetch_id,
                tenant_id="tenant-a",
                run_id="run-a",
                attempt_id="attempt-a",
                generation=3,
                max_body_bytes=1024,
                artifact_spool_dir=spool,
            )

    def _verify_worker_result(
        self, temporary: str, *, worker_changes=None, metadata_changes=None
    ):
        from urllib.parse import urlsplit

        result_dir = Path(temporary)
        url = "https://api.fixture.test/v1/artifact.pdf"
        acquired_at = datetime.now(UTC).isoformat()
        body_digest = "sha256:" + hashlib.sha256(self.body).hexdigest()
        metadata = self._metadata(
            source_id="controlled-http",
            source_revision_id="controlled-revision-1",
            policy_digest="sha256:" + "1" * 64,
            release_id="release-a",
            build_manifest_digest="sha256:" + "2" * 64,
            request_target_digest="sha256:" + hashlib.sha256(url.encode()).hexdigest(),
            effective_target_digest="sha256:"
            + hashlib.sha256(url.encode()).hexdigest(),
            request_origin="https://api.fixture.test",
            effective_origin="https://api.fixture.test",
            request_path_digest="sha256:"
            + hashlib.sha256(urlsplit(url).path.encode()).hexdigest(),
            effective_path_digest="sha256:"
            + hashlib.sha256(urlsplit(url).path.encode()).hexdigest(),
            media_type="application/pdf",
            acquired_at=acquired_at,
            **(metadata_changes or {}),
        )
        worker = {
            "fetch_id": self.fetch_id,
            "source_url": url,
            "evidence_ref": self.fetch_id,
            "body_sha256": body_digest.removeprefix("sha256:"),
            "body_size_bytes": len(self.body),
            "media_type": "application/pdf",
            "body_representation": "decoded-http-entity",
            "acquired_at": acquired_at,
            **(worker_changes or {}),
        }
        records = b""
        files = {
            "records.jsonl": records,
            "report.json": json.dumps(
                {
                    "state": "passed",
                    "requests": 1,
                    "pages": 1,
                    "records": 0,
                    "records_file": "records.jsonl",
                    "records_sha256": hashlib.sha256(records).hexdigest(),
                    "output_mode": "artifact",
                }
            ).encode(),
            "fetch-evidence.jsonl": (json.dumps(worker) + "\n").encode(),
            "raw-references.json": json.dumps(
                [
                    {
                        "evidence_ref": self.fetch_id,
                        "url": url,
                        "sha256": body_digest.removeprefix("sha256:"),
                    }
                ]
            ).encode(),
        }
        for name, content in files.items():
            (result_dir / name).write_bytes(content)
        plan = {
            "profile": "scrapy-live-http-v1",
            "source": {
                "source_id": "controlled-http",
                "revision": "controlled-revision-1",
                "origin": "https://api.fixture.test",
                "path_prefixes": ["/v1/"],
            },
            "budgets": {"max_requests": 1, "max_pages": 1, "max_records": 0},
            "fetch": {"inputs": {"url": url, "content_type": "application/pdf"}},
            "artifact_output": {"mode": "artifact"},
            "output_contract": {
                "mode": "artifact",
                "media_types": ["application/pdf"],
                "max_bytes": 1024,
            },
        }
        with mock.patch(
            "acquisition_application.live_evidence.build_opener",
            return_value=self._response(metadata),
        ):
            return verify_live_result_bundle(
                result_dir,
                tenant_id="tenant-a",
                run_id="run-a",
                attempt_id="attempt-a",
                generation=3,
                source_id="controlled-http",
                source_revision_id="controlled-revision-1",
                policy_digest="sha256:" + "1" * 64,
                release_id="release-a",
                build_manifest_digest="sha256:" + "2" * 64,
                gateway_evidence_url="https://gateway.fixture.test",
                gateway_evidence_token="test-only-token",
                compiled_plan=plan,
                trusted_bot_id="bot-a",
                max_response_bytes=1024,
                maximum_total_response_bytes=1024,
                maximum_requests=1,
                output_modality="artifact",
                artifact_media_types=("application/pdf",),
                max_artifact_bytes=1024,
            )

    def test_verified_evidence_is_spooled_and_bound_to_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            spool = Path(temporary)
            metadata, body_path, size, digest = self._fetch(spool, self._metadata())
            self.assertEqual(self.fetch_id, metadata["fetch_id"])
            self.assertEqual(self.body, body_path.read_bytes())
            self.assertEqual(len(self.body), size)
            self.assertEqual("sha256:" + hashlib.sha256(self.body).hexdigest(), digest)

    def test_cross_tenant_and_cross_attempt_evidence_are_rejected_and_cleaned(self):
        for changed in (
            {"tenant_id": "tenant-b"},
            {"run_id": "another-run"},
            {"attempt_id": "another-attempt"},
        ):
            with (
                self.subTest(changed=changed),
                tempfile.TemporaryDirectory() as temporary,
            ):
                spool = Path(temporary)
                with self.assertRaisesRegex(
                    FixtureImportError, "execution binding diverged"
                ):
                    self._fetch(spool, self._metadata(**changed))
                self.assertEqual([], list(spool.iterdir()))

    def test_interrupted_evidence_read_cleans_partial_spool(self):
        class Response:
            status = 200

            def __init__(self):
                self.headers = {}
                self.reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                self.reads += 1
                if self.reads == 1:
                    return b"partial"
                raise OSError("controlled read interruption")

        class Opener:
            def open(self, _request, timeout):
                return Response()

        with tempfile.TemporaryDirectory() as temporary:
            spool = Path(temporary)
            with (
                mock.patch(
                    "acquisition_application.live_evidence.build_opener",
                    return_value=Opener(),
                ),
                self.assertRaisesRegex(FixtureImportError, "unavailable"),
            ):
                _fetch_trusted_evidence(
                    endpoint="https://gateway.fixture.test",
                    token="test-only-token",
                    fetch_id=self.fetch_id,
                    tenant_id="tenant-a",
                    run_id="run-a",
                    attempt_id="attempt-a",
                    generation=3,
                    max_body_bytes=1024,
                    artifact_spool_dir=spool,
                )
            self.assertEqual([], list(spool.iterdir()))

    def test_discovery_evidence_cannot_be_used_as_gateway_fetch_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch(
                    "acquisition_application.live_evidence.build_opener",
                    side_effect=AssertionError("untrusted evidence id reached gateway"),
                ),
                self.assertRaisesRegex(FixtureImportError, "identity"),
            ):
                _fetch_trusted_evidence(
                    endpoint="https://gateway.fixture.test",
                    token="test-only-token",
                    fetch_id="manifest-" + "a" * 64,
                    tenant_id="tenant-a",
                    run_id="run-a",
                    attempt_id="attempt-a",
                    generation=3,
                    max_body_bytes=1024,
                    artifact_spool_dir=Path(temporary),
                )
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_worker_cannot_substitute_arbitrary_artifact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result_dir = Path(temporary)
            self._verify_worker_result(result_dir).cleanup()
            (result_dir / "agent-provided.body").write_bytes(b"untrusted bytes")
            with self.assertRaisesRegex(
                FixtureImportError, "bundle membership is invalid"
            ):
                self._verify_worker_result(result_dir)
            self.assertTrue((result_dir / "agent-provided.body").exists())

    def test_worker_declarations_cannot_replace_gateway_bytes_or_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._verify_worker_result(temporary)
            try:
                self.assertEqual(self.body, bundle.pages[0].body_path.read_bytes())
                self.assertEqual(
                    "sha256:" + hashlib.sha256(self.body).hexdigest(),
                    bundle.pages[0].digest,
                )
            finally:
                bundle.cleanup()

        mutations = (
            ({"body_sha256": "0" * 64}, None),
            ({"body_size_bytes": len(self.body) + 1}, None),
            ({"media_type": "text/plain"}, None),
            ({"body_representation": "wire-bytes"}, None),
            (None, {"body_digest": "sha256:" + "0" * 64}),
            (None, {"body_size": len(self.body) + 1}),
        )
        for worker_changes, metadata_changes in mutations:
            with (
                self.subTest(worker=worker_changes, gateway=metadata_changes),
                tempfile.TemporaryDirectory() as temporary,
            ):
                import tempfile as tempfile_module

                real_temporary_directory = tempfile_module.TemporaryDirectory
                allocated = []

                def tracked_temporary_directory(
                    *args,
                    _temporary_directory=real_temporary_directory,
                    _allocated=allocated,
                    **kwargs,
                ):
                    directory = _temporary_directory(*args, **kwargs)
                    _allocated.append(Path(directory.name))
                    return directory

                with (
                    mock.patch(
                        "acquisition_application.live_evidence.tempfile.TemporaryDirectory",
                        side_effect=tracked_temporary_directory,
                    ),
                    self.assertRaises(FixtureImportError),
                ):
                    self._verify_worker_result(
                        temporary,
                        worker_changes=worker_changes,
                        metadata_changes=metadata_changes,
                    )
                self.assertEqual([], list(Path(temporary).glob("evidence-*.body")))
                self.assertEqual(1, len(allocated))
                self.assertFalse(allocated[0].exists())


if __name__ == "__main__":
    unittest.main()
