from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from acquisition_application.application import TrustedAcquisitionApplication
from acquisition_application.context import TrustedExecutionContext
from acquisition_application.result_bundle import (
    FixtureImportError,
    VerifiedFixturePage,
    VerifiedResultBundle,
)
from acquisition_output.domain import ArtifactOutput, ProducerIdentity


class RawArtifactImportTests(unittest.TestCase):
    def test_only_verified_bound_bytes_are_custodied_and_staged_as_artifact_output(
        self,
    ):
        body = b"%PDF-1.7\x00raw\xff"
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        acquired = datetime.now(UTC)
        page = VerifiedFixturePage(
            url="https://source.fixture.test/files/report.pdf",
            evidence_ref="e" * 32,
            digest=digest,
            media_type="application/pdf",
            body=body,
            acquired_at=acquired,
            body_representation="decoded-http-entity",
        )
        bundle = VerifiedResultBundle(
            records=(),
            pages=(page,),
            records_digest=hashlib.sha256(b"").hexdigest(),
            total_bytes=len(body),
            trusted_bot_id="bot-raw",
            artifact_only=True,
            artifact_contract={
                "mode": "artifact",
                "media_types": ["application/pdf"],
                "max_bytes": 1024,
            },
        )
        context = TrustedExecutionContext(
            tenant_id="tenant-raw",
            run_id="run-raw",
            request_id="submission-raw",
            attempt_id="attempt-raw",
            generation=1,
            source_id="source-raw",
            source_revision_id="source-raw-v1",
            bot_id=None,
            build_id="build-raw",
            build_manifest_digest="sha256:" + "a" * 64,
            runtime_image_digest="sha256:" + "b" * 64,
            policy_digest="sha256:" + "c" * 64,
            maximum_total_response_bytes=2_000_000,
            maximum_requests=1,
            release_id="release-raw",
            created_at=acquired,
            deadline=acquired,
            execution_mode="live-authorized",
            binding_digest="d" * 64,
            output_modality="artifact",
            artifact_media_types=("application/pdf",),
            max_artifact_bytes=1024,
        )

        class App(TrustedAcquisitionApplication):
            def _verification_context(self, tenant_id, run_id):
                return context

        class Outputs:
            staged = None

            def create_output_set(self, spec, operation):
                self.spec = spec

            def stage_output(self, output):
                self.staged = output

            def seal_output_set(self, tenant_id, output_set_id, operation):
                return SimpleNamespace(manifest_digest="sha256:" + "f" * 64)

        class Custody:
            stored = None

            def store_verified_entity(self, **kwargs):
                self.stored = kwargs
                return kwargs["artifact"]

        outputs = Outputs()
        custody = Custody()
        app = App(None, outputs, custody)
        producer = ProducerIdentity(
            "bot-raw",
            "scrapy-live-http-v1",
            "raw-artifact",
            "v1",
            "release-raw",
            "sha256:" + "a" * 64,
        )
        output_set_id, manifest_digest = app.import_verified_artifact_result(
            "tenant-raw",
            "run-raw",
            bundle=bundle,
            producer=producer,
            retention_policy_ref="controlled-retention-v1",
        )
        self.assertTrue(output_set_id.startswith("set-"))
        self.assertEqual("sha256:" + "f" * 64, manifest_digest)
        self.assertEqual(body, custody.stored["body"])
        self.assertEqual(digest, custody.stored["artifact"].digest)
        self.assertEqual("decoded-http-entity", custody.stored["representation"])
        self.assertIsInstance(outputs.staged, ArtifactOutput)
        self.assertEqual((), outputs.spec.payload_contracts)
        self.assertEqual(1, outputs.spec.expected_output_count)

    def test_untrusted_artifact_metadata_is_rejected_before_custody_or_staging(self):
        body = b"%PDF-1.7\x00raw"
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        page = VerifiedFixturePage(
            url="https://source.fixture.test/files/report.pdf",
            evidence_ref="e" * 32,
            digest=digest,
            media_type="application/pdf",
            body=body,
            acquired_at=datetime.now(UTC),
            body_representation="decoded-http-entity",
        )
        bundle = VerifiedResultBundle(
            records=(),
            pages=(page,),
            records_digest=hashlib.sha256(b"").hexdigest(),
            total_bytes=len(body),
            trusted_bot_id="bot-raw",
            artifact_only=True,
        )
        context = SimpleNamespace(
            output_modality="artifact",
            artifact_media_types=("application/pdf",),
            max_artifact_bytes=1024,
        )

        class App(TrustedAcquisitionApplication):
            def _verification_context(self, tenant_id, run_id):
                return context

        class Outputs:
            called = False

            def create_output_set(self, spec, operation):
                self.called = True

            def stage_output(self, output):
                self.called = True

        class Custody:
            called = False

            def store_verified_entity(self, **kwargs):
                self.called = True

        producer = ProducerIdentity(
            "bot-raw",
            "scrapy-live-http-v1",
            "raw-artifact",
            "v1",
            "release-raw",
            "sha256:" + "a" * 64,
        )
        invalid_pages = (
            replace(page, media_type="text/plain"),
            replace(page, body_representation="wire-bytes"),
            replace(page, digest="sha256:" + "0" * 64),
            replace(
                page,
                body=b"x" * 1025,
                digest="sha256:" + hashlib.sha256(b"x" * 1025).hexdigest(),
            ),
        )
        for invalid_page in invalid_pages:
            with self.subTest(
                media_type=invalid_page.media_type,
                representation=invalid_page.body_representation,
                size=len(invalid_page.body),
                digest=invalid_page.digest,
            ):
                outputs = Outputs()
                custody = Custody()
                app = App(None, outputs, custody)
                invalid_bundle = replace(bundle, pages=(invalid_page,))
                with self.assertRaisesRegex(
                    FixtureImportError, "trusted output constraints"
                ):
                    app.import_verified_artifact_result(
                        "tenant-raw",
                        "run-raw",
                        bundle=invalid_bundle,
                        producer=producer,
                        retention_policy_ref="controlled-retention-v1",
                    )
                self.assertFalse(custody.called)
                self.assertFalse(outputs.called)

    def test_record_binding_rejects_artifact_result_before_custody(self):
        class App(TrustedAcquisitionApplication):
            def _verification_context(self, tenant_id, run_id):
                return SimpleNamespace(
                    output_modality="record", max_artifact_bytes=None
                )

        class Custody:
            called = False

            def store_verified_entity(self, **kwargs):
                self.called = True

        custody = Custody()
        app = App(None, None, custody)
        bundle = VerifiedResultBundle(
            (), (), hashlib.sha256(b"").hexdigest(), 0, artifact_only=True
        )
        with self.assertRaisesRegex(ValueError, "BC04 output binding"):
            app.import_verified_artifact_result(
                "tenant",
                "run",
                bundle=bundle,
                producer=ProducerIdentity(
                    "bot",
                    "runtime",
                    "capability",
                    "v1",
                    "release",
                    "sha256:" + "a" * 64,
                ),
                retention_policy_ref="retention",
            )
        self.assertFalse(custody.called)


if __name__ == "__main__":
    unittest.main()
