import hashlib
import json
import unittest
from pathlib import Path

from collector_engineering.live_admission import validate_live_admission
from collector_engineering.syntax import PlaybookError
from jsonschema import Draft202012Validator, FormatChecker
from mozaic_bot_build import canonical_json

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"


class LiveAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "contract_version": "source-revision/v1",
            "source_id": "controlled-source",
            "revision_id": "revision-1",
            "owner": "test-owner",
            "status": "eligible",
            "scope": {
                "origins": ["https://example.test"],
                "methods": ["GET"],
                "path_prefixes": ["/api/v1"],
                "query_parameter_values": {"top": ["10"]},
            },
            "configuration_version": "1.0.0",
            "configuration_digest": "",
        }
        self.source["configuration_digest"] = self._digest(
            self.source, "configuration_digest"
        )
        self.policy = {
            "contract_version": "acquisition-policy/v1",
            "policy_id": "policy-test",
            "policy_version": "1.0.0",
            "source_id": "controlled-source",
            "source_revision_id": "revision-1",
            "status": "eligible",
            "origins": ["https://example.test"],
            "methods": ["GET"],
            "path_prefixes": ["/api/v1/reports"],
            "query_parameter_values": {"top": ["10"]},
            "dns_policy": "public-addresses-only",
            "redirect_policy": "deny",
            "max_requests": 2,
            "max_concurrency": 1,
            "requests_per_second": 2,
            "max_response_bytes": 32768,
            "max_total_response_bytes": 65536,
            "deadline_seconds": 30,
            "require_tls": True,
            "credential_refs": [],
            "evidence_retention_policy_ref": "local-test-retain-30d",
            "policy_digest": "",
        }
        self.policy["policy_digest"] = self._digest(self.policy, "policy_digest")
        self.manifest = {
            "build_id": "build-1",
            "source": {
                "source_id": "controlled-source",
                "source_revision": "revision-1",
            },
        }
        self.release = {
            "contract_version": "bot-release/v1",
            "release_id": "release-1",
            "status": "approved",
            "bot_id": "bot-1",
            "build_id": "build-1",
            "build_manifest_digest": "sha256:"
            + hashlib.sha256(canonical_json(self.manifest)).hexdigest(),
            "backend_id": "scrapy-live-http",
            "backend_version": "0.1.0",
            "runtime_profile_id": "scrapy-live-http-v1",
            "runtime_image_digest": "sha256:" + "c" * 64,
            "source_id": "controlled-source",
            "source_revision_id": "revision-1",
            "policy_id": "policy-test",
            "policy_version": "1.0.0",
            "compiler_compatibility": "collector-ir/v1",
            "approved_at": "2026-11-25T12:00:00Z",
            "approval_evidence_ref": "test:approval",
            "release_digest": "",
        }
        self.release["release_digest"] = self._digest(self.release, "release_digest")

    @staticmethod
    def _digest(document, field):
        unsigned = {key: value for key, value in document.items() if key != field}
        return "sha256:" + hashlib.sha256(canonical_json(unsigned)).hexdigest()

    def test_contract_schemas_are_valid_and_binding_fixtures_match(self):
        documents = (
            ("source-revision/draft/v1/SourceRevision.schema.json", self.source),
            ("acquisition-policy/draft/v1/AcquisitionPolicy.schema.json", self.policy),
            ("bot-release/draft/v1/BotRelease.schema.json", self.release),
            (
                "fetch-evidence/draft/v1/FetchEvidence.schema.json",
                json.loads(
                    (
                        CONTRACTS
                        / "fetch-evidence/draft/v1/examples/controlled-fetch-evidence.json"
                    ).read_text()
                ),
            ),
        )
        for relative, instance in documents:
            with self.subTest(contract=relative):
                schema = json.loads((CONTRACTS / relative).read_text())
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema, format_checker=FormatChecker()).validate(
                    instance
                )

    def test_admission_binds_release_build_source_and_policy(self):
        result = validate_live_admission(
            source_revision=self.source,
            policy=self.policy,
            release=self.release,
            build_manifest=self.manifest,
            requested_urls=("https://example.test/api/v1/reports?top=10",),
        )
        self.assertEqual(result.release_id, "release-1")
        self.assertEqual(result.policy_id, "policy-test")

    def test_revoked_or_mismatched_documents_fail_closed(self):
        cases = (
            ({**self.source, "status": "revoked"}, self.policy, self.release),
            (
                self.source,
                {**self.policy, "source_revision_id": "revision-2"},
                self.release,
            ),
            (self.source, self.policy, {**self.release, "status": "revoked"}),
            (self.source, self.policy, {**self.release, "policy_version": "2.0.0"}),
        )
        for source, policy, release in cases:
            with (
                self.subTest(source=source, policy=policy, release=release),
                self.assertRaises(PlaybookError),
            ):
                validate_live_admission(
                    source_revision=source,
                    policy=policy,
                    release=release,
                    build_manifest=self.manifest,
                    requested_urls=("https://example.test/api/v1/reports",),
                )

    def test_policy_scope_and_untrusted_origin_are_rejected(self):
        for url in (
            "https://example.test/admin",
            "http://example.test/api/v1/reports",
            "https://example.test.evil.invalid/api/v1/reports",
            "https://user:password@example.test/api/v1/reports",
            "https://example.test/api/v1/reports?top=1000",
            "https://example.test/api/v1/reports?top=10&top=10",
            "https://example.test/api/v1/reports/%2e%2e/admin",
            "https://example.test/api/v1/reports/%252e%252e/admin",
            "https://example.test/api/v1/reports%2fadmin",
            "https://example.test/api/v1/reports?top=%252fadmin",
            "https://example.test/api/v1/reports?top=%zz",
            "https://example.test:bad/api/v1/reports",
            "https://example.test/api/v1/reports#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(PlaybookError):
                validate_live_admission(
                    source_revision=self.source,
                    policy=self.policy,
                    release=self.release,
                    build_manifest=self.manifest,
                    requested_urls=(url,),
                )


if __name__ == "__main__":
    unittest.main()
