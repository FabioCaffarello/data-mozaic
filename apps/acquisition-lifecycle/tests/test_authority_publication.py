from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

import rfc8785
from acquisition_lifecycle.application import AcquisitionLifecycle
from acquisition_lifecycle.domain import LifecycleError


class AuthorityStore:
    def __init__(self) -> None:
        self.publications: list[dict[str, Any]] = []
        self.fail_mark_once = False

    def pending_authority(self, authority_id: str) -> dict[str, Any] | None:
        pending = [
            item
            for item in self.publications
            if item["authority_id"] == authority_id and item["state"] == "pending"
        ]
        return dict(pending[0]) if pending else None

    def latest_authority_generation(self, authority_id: str) -> int:
        generations = [
            item["generation"]
            for item in self.publications
            if item["authority_id"] == authority_id
        ]
        return max(generations, default=0)

    def prepare_authority(
        self,
        authority_id: str,
        generation: int,
        snapshot_digest: str,
        envelope_digest: str,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        item = {
            "authority_id": authority_id,
            "generation": generation,
            "snapshot_digest": snapshot_digest,
            "envelope_digest": envelope_digest,
            "signed_envelope": envelope,
            "state": "pending",
        }
        self.publications.append(item)
        return dict(item)

    def supersede_expired_authority(
        self, authority_id: str, generation: int, envelope_digest: str
    ) -> None:
        for item in self.publications:
            if (
                item["authority_id"] == authority_id
                and item["generation"] == generation
                and item["state"] == "pending"
                and item["envelope_digest"] == envelope_digest
            ):
                item["state"] = "superseded"
                return
        raise LifecycleError("pending publication was not found")

    def mark_authority_installed(self, authority_id: str, generation: int) -> None:
        if self.fail_mark_once:
            self.fail_mark_once = False
            raise RuntimeError("injected local confirmation loss")
        for item in self.publications:
            if (
                item["authority_id"] == authority_id
                and item["generation"] == generation
            ):
                item["state"] = "installed"
                return
        raise LifecycleError("publication was not found")

    def list_sources(self) -> list[dict[str, Any]]:
        return []

    def list_policies(self) -> list[dict[str, Any]]:
        return []

    def list_releases(self) -> list[dict[str, Any]]:
        return []


class AuthorityRegistry:
    def __init__(self) -> None:
        self.head: tuple[int, str] | None = None
        self.fail_before_install = False
        self.fail_after_install_once = False
        self.install_count = 0

    def current_generation(self, authority_id: str) -> int:
        return self.head[0] if self.head else 0

    def current_snapshot(self, authority_id: str) -> tuple[int, str] | None:
        return self.head

    def install(self, envelope: bytes, trusted_public_keys: object) -> dict[str, Any]:
        if self.fail_before_install:
            raise RuntimeError("injected BC04 installation failure")
        parsed = __import__("json").loads(envelope)
        snapshot = parsed["snapshot"]
        digest = "sha256:" + hashlib.sha256(rfc8785.dumps(snapshot)).hexdigest()
        proposed = (snapshot["generation"], digest)
        if (
            self.head is not None
            and self.head[0] == proposed[0]
            and self.head != proposed
        ):
            raise LifecycleError("same-generation authority conflict")
        self.head = proposed
        self.install_count += 1
        if self.fail_after_install_once:
            self.fail_after_install_once = False
            raise RuntimeError("injected acknowledgment loss after BC04 install")
        return snapshot


class FakeContracts:
    def resolve(self, reference: object) -> None:
        raise AssertionError("contract resolution is not part of authority publication")


class FakeSigner:
    key_id = "test-key"

    def sign(self, canonical_snapshot: bytes) -> str:
        return "test-signature"


class FakeApproval:
    def assert_can_approve(self, actor_id: str, release_id: str) -> str:
        raise AssertionError("approval is not part of authority publication")


class AuthorityPublicationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AuthorityStore()
        self.registry = AuthorityRegistry()
        self.app = AcquisitionLifecycle(
            self.store,
            FakeContracts(),
            self.registry,
            FakeSigner(),
            {},
            FakeApproval(),
            authority_id="test-authority",
        )
        self.now = datetime(2026, 11, 25, 12, tzinfo=UTC)

    def test_retry_after_bc04_acceptance_recovers_lost_local_confirmation(self) -> None:
        self.registry.fail_after_install_once = True
        with self.assertRaisesRegex(RuntimeError, "acknowledgment loss"):
            self.app.publish_authority_snapshot(now=self.now)

        pending_envelope = self.store.pending_authority("test-authority")[
            "signed_envelope"
        ]
        envelope, authority = self.app.publish_authority_snapshot(
            now=self.now + timedelta(hours=3)
        )

        self.assertEqual(envelope, rfc8785.dumps(pending_envelope))
        self.assertIsNone(authority)
        self.assertEqual(self.registry.install_count, 1)
        self.assertEqual(self.store.publications[0]["state"], "installed")

    def test_retry_after_local_mark_failure_does_not_reinstall_expired_snapshot(
        self,
    ) -> None:
        self.store.fail_mark_once = True
        with self.assertRaisesRegex(RuntimeError, "local confirmation loss"):
            self.app.publish_authority_snapshot(now=self.now)

        envelope, authority = self.app.publish_authority_snapshot(
            now=self.now + timedelta(hours=3)
        )

        self.assertIsNone(authority)
        self.assertEqual(self.registry.install_count, 1)
        self.assertEqual(self.store.publications[0]["state"], "installed")
        self.assertEqual(
            rfc8785.dumps(self.store.publications[0]["signed_envelope"]), envelope
        )

    def test_expired_uninstalled_snapshot_is_superseded_without_reusing_generation(
        self,
    ) -> None:
        self.registry.fail_before_install = True
        with self.assertRaisesRegex(RuntimeError, "installation failure"):
            self.app.publish_authority_snapshot(now=self.now)

        self.registry.fail_before_install = False
        envelope, _authority = self.app.publish_authority_snapshot(
            now=self.now + timedelta(hours=3)
        )
        snapshot = __import__("json").loads(envelope)["snapshot"]

        self.assertEqual(snapshot["generation"], 2)
        self.assertEqual(self.store.publications[0]["state"], "superseded")
        self.assertEqual(self.store.publications[1]["state"], "installed")

    def test_same_generation_with_different_bc04_digest_fails_closed(self) -> None:
        self.registry.fail_before_install = True
        with self.assertRaisesRegex(RuntimeError, "installation failure"):
            self.app.publish_authority_snapshot(now=self.now)
        self.registry.fail_before_install = False
        self.registry.head = (1, "sha256:" + "0" * 64)

        with self.assertRaisesRegex(LifecycleError, "conflicting authority snapshot"):
            self.app.publish_authority_snapshot(now=self.now)

    def test_naive_publication_time_is_rejected(self) -> None:
        with self.assertRaisesRegex(LifecycleError, "timezone"):
            self.app.publish_authority_snapshot(now=datetime(2026, 11, 25, 12))


if __name__ == "__main__":
    unittest.main()
