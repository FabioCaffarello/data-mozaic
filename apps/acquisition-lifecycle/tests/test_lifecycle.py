from __future__ import annotations

import unittest
from typing import Any

from acquisition_lifecycle.application import AcquisitionLifecycle
from acquisition_lifecycle.domain import LifecycleError, canonical_digest


class MemoryStore:
    def __init__(self) -> None:
        self.sources: dict[tuple[str, str], dict[str, Any]] = {}
        self.policies: dict[tuple[str, str], dict[str, Any]] = {}

    def register_source(self, document: dict[str, Any], actor_id: str) -> str:
        key = (document["source_id"], document["revision_id"])
        digest = canonical_digest(document)
        existing = self.sources.get(key)
        if existing and existing["_revision_digest"] != digest:
            raise LifecycleError(
                "source revision identity conflicts with immutable content"
            )
        self.sources.setdefault(key, {**document, "_revision_digest": digest})
        return digest

    def resolve_source(self, source_id: str, revision_id: str) -> dict[str, Any]:
        value = self.sources.get((source_id, revision_id))
        if value is None:
            raise LifecycleError("source revision is unresolved")
        return dict(value)

    def set_source_state(
        self, source_id: str, revision_id: str, state: str, actor_id: str
    ) -> None:
        self.sources[(source_id, revision_id)]["status"] = state

    def register_policy(self, document: dict[str, Any], actor_id: str) -> str:
        key = (document["policy_id"], document["policy_version"])
        digest = canonical_digest(document)
        existing = self.policies.get(key)
        if existing and existing["_revision_digest"] != digest:
            raise LifecycleError(
                "policy identity/version conflicts with immutable content"
            )
        self.policies.setdefault(key, {**document, "_revision_digest": digest})
        return digest

    def resolve_policy(self, policy_id: str, version: str) -> dict[str, Any]:
        value = self.policies.get((policy_id, version))
        if value is None:
            raise LifecycleError("policy revision is unresolved")
        return dict(value)

    def set_policy_state(
        self, policy_id: str, version: str, state: str, actor_id: str
    ) -> None:
        self.policies[(policy_id, version)]["status"] = state


class NoContracts:
    def resolve(self, reference: object) -> None:
        raise AssertionError("contract lookup is not expected in these tests")


class NoApproval:
    def assert_can_approve(self, actor_id: str, release_id: str) -> str:
        raise AssertionError("approval is not expected in these tests")


class NoSigner:
    key_id = "test"

    def sign(self, canonical_snapshot: bytes) -> str:
        raise AssertionError("signing is not expected in these tests")


class NoAuthority:
    def current_generation(self, authority_id: str) -> int:
        raise AssertionError("authority publication is not expected in these tests")

    def install(self, envelope: bytes, trusted_public_keys: object) -> object:
        raise AssertionError("authority publication is not expected in these tests")


class AcquisitionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore()
        self.app = AcquisitionLifecycle(
            self.store,
            NoContracts(),
            NoAuthority(),
            NoSigner(),
            {},
            NoApproval(),
            authority_id="test-authority",
        )

    @staticmethod
    def source(**changes: Any) -> dict[str, Any]:
        document: dict[str, Any] = {
            "contract_version": "source-revision/v1",
            "source_id": "controlled-source",
            "revision_id": "revision-1",
            "owner": "Data Mozaic test",
            "status": "eligible",
            "scope": {
                "origins": ["https://example.test"],
                "methods": ["GET"],
                "path_prefixes": ["/v1/items"],
                "query_parameter_values": {"page": ["1", "2"]},
            },
            "configuration_version": "1.0.0",
            "configuration_digest": "",
        }
        document.update(changes)
        document["configuration_digest"] = canonical_digest(
            {
                key: value
                for key, value in document.items()
                if key != "configuration_digest"
            }
        )
        return document

    @staticmethod
    def policy(**changes: Any) -> dict[str, Any]:
        document: dict[str, Any] = {
            "contract_version": "acquisition-policy/v1",
            "policy_id": "controlled-policy",
            "policy_version": "1.0.0",
            "source_id": "controlled-source",
            "source_revision_id": "revision-1",
            "status": "eligible",
            "origins": ["https://example.test"],
            "methods": ["GET"],
            "path_prefixes": ["/v1/items/page"],
            "query_parameter_values": {"page": ["1"]},
            "dns_policy": "public-addresses-only",
            "test_destination_cidrs": [],
            "redirect_policy": "deny",
            "max_requests": 2,
            "max_concurrency": 1,
            "requests_per_second": 1,
            "max_response_bytes": 1000,
            "max_total_response_bytes": 2000,
            "deadline_seconds": 10,
            "require_tls": True,
            "credential_refs": [],
            "evidence_retention_policy_ref": "retention-v1",
            "policy_digest": "",
        }
        document.update(changes)
        document["policy_digest"] = canonical_digest(
            {key: value for key, value in document.items() if key != "policy_digest"}
        )
        return document

    def test_source_revision_is_idempotent_and_conflict_is_rejected(self) -> None:
        source = self.source()
        first = self.app.register_source_revision(source, actor_id="operator-1")
        second = self.app.register_source_revision(source, actor_id="operator-2")
        self.assertEqual(first, second)
        changed = {**source, "owner": "other owner", "configuration_digest": ""}
        changed["configuration_digest"] = canonical_digest(
            {
                key: value
                for key, value in changed.items()
                if key != "configuration_digest"
            }
        )
        with self.assertRaisesRegex(LifecycleError, "conflicts with immutable content"):
            self.app.register_source_revision(changed, actor_id="operator-1")

    def test_policy_requires_an_existing_eligible_source(self) -> None:
        with self.assertRaisesRegex(LifecycleError, "unresolved"):
            self.app.register_policy_revision(self.policy(), actor_id="operator-1")

    def test_policy_scope_must_be_within_source_scope(self) -> None:
        source = self.source()
        self.app.register_source_revision(source, actor_id="operator-1")
        policy = self.policy(path_prefixes=["/v1/items-evil"])
        with self.assertRaisesRegex(LifecycleError, "path scope"):
            self.app.register_policy_revision(policy, actor_id="operator-1")

    def test_policy_may_narrow_source_scope(self) -> None:
        source = self.source()
        self.app.register_source_revision(source, actor_id="operator-1")
        reference = self.app.register_policy_revision(
            self.policy(), actor_id="operator-1"
        )
        self.assertEqual(reference.policy_id, "controlled-policy")

    def test_policy_cannot_widen_query_values(self) -> None:
        source = self.source()
        self.app.register_source_revision(source, actor_id="operator-1")
        policy = self.policy(query_parameter_values={"page": ["3"]})
        with self.assertRaisesRegex(LifecycleError, "query scope"):
            self.app.register_policy_revision(policy, actor_id="operator-1")

    def test_release_v3_binds_both_input_and_payload_contracts(self) -> None:
        candidate = {
            "release_id": "release-with-input",
            "source": {"source_id": "controlled-source", "revision_id": "revision-1"},
            "policy": {"policy_id": "controlled-policy", "policy_version": "1.0.0"},
            "build": {
                "build_id": "a" * 64,
                "manifest_digest": "sha256:" + "b" * 64,
                "runtime_image_digest": "sha256:" + "c" * 64,
            },
            "bot_id": "bot-test",
            "payload_contract": {
                "contract_id": "controlled.record",
                "version": "1.0.0",
                "digest": "sha256:" + "d" * 64,
            },
            "input_contract": {
                "contract_id": "controlled.input",
                "version": "1.0.0",
                "digest": "sha256:" + "e" * 64,
            },
            "input_request": {
                "method": "GET",
                "url": "https://example.test/api/v1/items",
                "input_query": {"parameter": "limit", "query_name": "limit"},
            },
        }
        release = self.app._release_document(
            candidate,
            status="approved",
            actor_id="operator-test",
            evidence_ref="test:approval",
        )
        self.assertEqual("bot-release/v3", release["contract_version"])
        self.app._validate_release_schema(release)
        release.pop("input_contract")
        with self.assertRaisesRegex(LifecycleError, "structurally invalid"):
            self.app._validate_release_schema(release)


if __name__ == "__main__":
    unittest.main()
