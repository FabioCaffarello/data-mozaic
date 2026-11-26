"""Real PostgreSQL tests for durable BC04 coordination invariants."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import rfc8785
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.execution_capability import (
    ExecutionCapabilitySigner,
    verify_execution_capability,
)
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from execution_coordination.adapters.publication_grant import (
    Ed25519PublicationGrantSigner,
)
from execution_coordination.adapters.trusted_live_authority import (
    TrustedLiveAuthority,
)
from execution_coordination.domain import CoordinationError, ExecutionBinding
from execution_coordination.durable_application import (
    DurableExecutionCoordinator,
    FinalizationRecoveryState,
)
from execution_coordination.durable_domain import (
    DurableAttemptState,
    DurableRunIntent,
    DurableRunState,
    FailureDisposition,
    LiveExecutionBinding,
)


def fixture_binding() -> ExecutionBinding:
    digest = "a" * 64
    return ExecutionBinding(
        source_id="synthetic-fixture-source",
        source_revision="fixture-v1",
        bot_id="bot-" + digest,
        build_id=digest,
        bot_build_manifest_digest=digest,
        assembly_id=digest,
        catalog_digest=digest,
        oci_image="registry.invalid/fixture@sha256:" + digest,
        oci_digest="sha256:" + digest,
        local_image_id="sha256:" + digest,
        fixture_bundle_id="synthetic-fixture-bundle",
        fixture_bundle_digest=digest,
    )


class PostgresDurableIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dsn = os.environ.get("BC04_DATABASE_URL")
        if not dsn:
            raise RuntimeError("BC04_DATABASE_URL is required for integration tests")
        cls.repository = PostgresDurableRunRepository(dsn)
        cls.repository.get("integration-probe", str(uuid4()))

    def create_run(self, *, max_attempts: int = 3):
        run_id = str(uuid4())
        intent = DurableRunIntent(
            tenant_id="integration-tenant",
            run_id=run_id,
            request_id="request-" + uuid4().hex,
            idempotency_key="create-" + uuid4().hex,
            binding=fixture_binding(),
            deadline=datetime.now(UTC) + timedelta(minutes=2),
            max_attempts=max_attempts,
        )
        return intent, self.repository.create(intent)

    def _reserve_fixture_finalization(self):
        intent, _ = self.create_run()
        lease = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "lease-" + uuid4().hex,
            "claim-" + uuid4().hex,
            30,
        )
        signer = Ed25519PublicationGrantSigner(b"r" * 32, key_id="recovery-test-v1")
        grant = signer.issue(
            {
                "tenant_id": intent.tenant_id,
                "run_id": intent.run_id,
                "attempt_id": lease.attempt_id,
                "fencing_generation": lease.generation,
                "output_set_id": "set-recovery-" + uuid4().hex,
                "manifest_digest": "sha256:" + "9" * 64,
                "reservation_id": "res-"
                + hashlib.sha256(intent.run_id.encode()).hexdigest(),
                "operation_id": "publish-recovery-" + uuid4().hex,
                "issued_at": datetime.now(UTC).isoformat(),
            }
        )
        reservation = self.repository.reserve_finalization(lease, grant)
        return intent, reservation

    def test_bounded_reconciliation_keeps_unavailable_reservation_pending(self):
        intent, reservation = self._reserve_fixture_finalization()

        class UnavailablePublication:
            def get_publication_receipt(self, _tenant, _set_id):
                raise TimeoutError("synthetic BC05 timeout")

            def publish_grant(self, _grant):
                raise AssertionError("must not publish after receipt lookup timed out")

        coordinator = DurableExecutionCoordinator(
            self.repository, UnavailablePublication(), grant_signer=None
        )
        results = coordinator.reconcile(
            limit=1,
            timeout_seconds=1,
            tenant_id=intent.tenant_id,
            run_id=intent.run_id,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].state, FinalizationRecoveryState.UNRESOLVED)
        self.assertEqual(results[0].reason_code, "TimeoutError")
        pending = self.repository.get(intent.tenant_id, intent.run_id)
        self.assertEqual(pending.state, DurableRunState.FINALIZATION_PENDING)
        self.assertEqual(pending.reservation, reservation)

    def test_reconciliation_classifies_mismatched_receipt_without_resolving_reservation(
        self,
    ):
        intent, reservation = self._reserve_fixture_finalization()
        mismatched = {
            "contract_version": "publication-receipt/v1",
            "tenant_id": intent.tenant_id,
            "operation_id": reservation.operation_id,
            "output_set_id": reservation.output_set_id,
            "manifest_digest": "sha256:" + "8" * 64,
            "publication_id": "pub-mismatch",
            "published_at": datetime.now(UTC).isoformat(),
            "finalization_reservation_id": reservation.reservation_id,
        }

        class ConflictingPublication:
            def get_publication_receipt(self, _tenant, _set_id):
                return mismatched

            def publish_grant(self, _grant):
                raise AssertionError("must not replay after a mismatched receipt")

        coordinator = DurableExecutionCoordinator(
            self.repository, ConflictingPublication(), grant_signer=None
        )
        results = coordinator.reconcile(
            limit=1,
            timeout_seconds=1,
            tenant_id=intent.tenant_id,
            run_id=intent.run_id,
        )
        self.assertEqual(results[0].state, FinalizationRecoveryState.CONFLICT)
        pending = self.repository.get(intent.tenant_id, intent.run_id)
        self.assertEqual(pending.state, DurableRunState.FINALIZATION_PENDING)
        self.assertIsNone(pending.publication_receipt)

    def test_create_and_claim_replay_are_durable(self):
        intent, first = self.create_run()
        self.assertEqual(first.state, DurableRunState.ACCEPTED)
        self.assertEqual(self.repository.create(intent), first)
        operation_id = "claim-" + uuid4().hex
        lease = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "secret-lease-token",
            operation_id,
            30,
        )
        replay = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "different-generated-token",
            operation_id,
            30,
        )
        self.assertEqual(lease.generation, 1)
        self.assertEqual(replay, lease)
        self.assertEqual(
            self.repository.get(intent.tenant_id, intent.run_id).attempt_id,
            lease.attempt_id,
        )

    def test_create_idempotency_rejects_changed_intent(self):
        intent, _ = self.create_run()
        changed = DurableRunIntent(
            tenant_id=intent.tenant_id,
            run_id=str(uuid4()),
            request_id=intent.request_id,
            idempotency_key=intent.idempotency_key,
            binding=intent.binding,
            deadline=intent.deadline,
            max_attempts=intent.max_attempts,
        )
        with self.assertRaises(CoordinationError):
            self.repository.create(changed)

    def test_independent_clients_serialize_attempt_acquisition(self):
        intent, _ = self.create_run()
        barrier = threading.Barrier(2)

        def claim(worker: str):
            repo = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"])
            barrier.wait(timeout=5)
            try:
                return repo.claim(
                    intent.tenant_id,
                    intent.run_id,
                    worker,
                    "token-" + intent.run_id + worker,
                    "op-" + intent.run_id + worker,
                    30,
                )
            except CoordinationError as error:
                return ("error", str(error))

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, ("worker-a", "worker-b")))
        self.assertEqual(
            sum(
                not (isinstance(value, tuple) and value[0] == "error")
                for value in outcomes
            ),
            1,
            [
                value[1] if isinstance(value, tuple) else type(value).__name__
                for value in outcomes
            ],
        )
        snapshot = self.repository.get(intent.tenant_id, intent.run_id)
        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.attempt_count, 1)

    def test_expired_lease_gets_new_monotonic_generation(self):
        intent, _ = self.create_run()
        first = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "token-1",
            "claim-1-" + uuid4().hex,
            1,
        )
        time.sleep(1.1)
        second = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "token-2",
            "claim-2-" + uuid4().hex,
            30,
        )
        self.assertEqual(second.generation, first.generation + 1)
        with self.assertRaises(CoordinationError):
            self.repository.fail_attempt(
                first,
                FailureDisposition.PERMANENT,
                "STALE_WORKER",
                "stale-" + uuid4().hex,
            )

    def test_scoped_recovery_expires_only_requested_run_and_is_idempotent(self):
        intent, _ = self.create_run()
        unrelated, _ = self.create_run()
        lease = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "expired-token-" + uuid4().hex,
            "claim-expiring-" + uuid4().hex,
            1,
        )

        class NoPublication:
            def publish_grant(self, _grant):
                raise AssertionError("lease recovery must not publish")

            def get_publication_receipt(self, _tenant_id, _set_id):
                raise AssertionError("lease recovery must not inspect BC05")

        coordinator = DurableExecutionCoordinator(
            self.repository, NoPublication(), grant_signer=None
        )
        with self.assertRaises(CoordinationError):
            coordinator.recover_expired_run("different-tenant", intent.run_id)
        self.assertEqual(
            self.repository.get(intent.tenant_id, intent.run_id).attempt_state,
            DurableAttemptState.RUNNING,
        )
        time.sleep(1.1)
        barrier = threading.Barrier(2)

        def recover_from_independent_client():
            independent = DurableExecutionCoordinator(
                PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"]),
                NoPublication(),
                grant_signer=None,
            )
            barrier.wait(timeout=5)
            return independent.recover_expired_run(intent.tenant_id, intent.run_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            recovered, concurrent_replay = list(
                pool.map(lambda _index: recover_from_independent_client(), range(2))
            )
        self.assertEqual(concurrent_replay, recovered)
        self.assertEqual(recovered.state, DurableRunState.ACCEPTED)
        self.assertEqual(recovered.attempt_state, DurableAttemptState.EXPIRED)
        self.assertEqual(recovered.generation, lease.generation)
        self.assertIsNone(recovered.reservation)

        replay = coordinator.recover_expired_run(intent.tenant_id, intent.run_id)
        self.assertEqual(replay, recovered)
        self.assertEqual(
            self.repository.get(unrelated.tenant_id, unrelated.run_id).state,
            DurableRunState.ACCEPTED,
        )
        with self.assertRaises(CoordinationError):
            self.repository.fail_attempt(
                lease,
                FailureDisposition.RETRYABLE,
                "STALE_RESULT",
                "stale-recovery-" + uuid4().hex,
            )

    def test_scoped_recovery_times_out_at_database_run_deadline(self):
        run_id = str(uuid4())
        intent = DurableRunIntent(
            tenant_id="integration-tenant",
            run_id=run_id,
            request_id="request-" + uuid4().hex,
            idempotency_key="deadline-" + uuid4().hex,
            binding=fixture_binding(),
            deadline=datetime.now(UTC) + timedelta(seconds=1),
            max_attempts=2,
        )
        self.repository.create(intent)
        lease = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "deadline-token-" + uuid4().hex,
            "deadline-claim-" + uuid4().hex,
            30,
        )
        time.sleep(1.1)
        recovered = self.repository.recover_expired_run(intent.tenant_id, run_id)
        self.assertEqual(recovered.state, DurableRunState.TIMED_OUT)
        self.assertEqual(recovered.attempt_state, DurableAttemptState.EXPIRED)
        self.assertIsNone(recovered.lease_until)
        self.assertEqual(recovered.generation, lease.generation)

    def test_reserved_finalization_survives_cancellation_and_receipt_replay(self):
        intent, _ = self.create_run()
        lease = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "token-finalize",
            "claim-finalize-" + uuid4().hex,
            30,
        )
        operation_id = "publish-" + uuid4().hex
        digest = "sha256:" + "b" * 64
        signer = Ed25519PublicationGrantSigner(b"x" * 32, key_id="integration-key")
        grant = signer.issue(
            {
                "tenant_id": intent.tenant_id,
                "run_id": intent.run_id,
                "attempt_id": lease.attempt_id,
                "fencing_generation": lease.generation,
                "output_set_id": "set-" + uuid4().hex,
                "manifest_digest": digest,
                "reservation_id": "res-"
                + hashlib.sha256(intent.run_id.encode()).hexdigest(),
                "operation_id": operation_id,
                "issued_at": datetime.now(UTC).isoformat(),
            }
        )
        reservation = self.repository.reserve_finalization(lease, grant)
        replayed_reservation = self.repository.reserve_finalization(lease, grant)
        self.assertEqual(replayed_reservation, reservation)
        changed_intent = signer.issue(
            {
                "tenant_id": intent.tenant_id,
                "run_id": intent.run_id,
                "attempt_id": lease.attempt_id,
                "fencing_generation": lease.generation,
                "output_set_id": reservation.output_set_id,
                "manifest_digest": "sha256:" + "c" * 64,
                "reservation_id": reservation.reservation_id,
                "operation_id": operation_id,
                "issued_at": datetime.now(UTC).isoformat(),
            }
        )
        with self.assertRaises(CoordinationError):
            self.repository.reserve_finalization(lease, changed_intent)
        self.repository.cancel(intent.tenant_id, intent.run_id, "cancel-" + uuid4().hex)
        receipt = {
            "contract_version": "publication-receipt/v1",
            "tenant_id": intent.tenant_id,
            "operation_id": operation_id,
            "output_set_id": reservation.output_set_id,
            "manifest_digest": digest,
            "publication_id": "pub-" + uuid4().hex,
            "published_at": datetime.now(UTC).isoformat(),
            "finalization_reservation_id": reservation.reservation_id,
        }
        completed = self.repository.complete_finalization(
            intent.tenant_id,
            intent.run_id,
            reservation.reservation_id,
            receipt,
            "complete-" + uuid4().hex,
        )
        self.assertEqual(completed.state, DurableRunState.SUCCEEDED)
        self.assertIsNotNone(completed.cancel_requested_at)
        self.assertEqual(completed.publication_receipt, receipt)

    def test_cancel_before_reservation_prevents_publication_authorization(self):
        intent, _ = self.create_run()
        lease = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "token-cancel-first",
            "claim-cancel-first-" + uuid4().hex,
            30,
        )
        self.repository.cancel(
            intent.tenant_id, intent.run_id, "cancel-first-" + uuid4().hex
        )
        signer = Ed25519PublicationGrantSigner(b"y" * 32, key_id="integration-key")
        grant = signer.issue(
            {
                "tenant_id": intent.tenant_id,
                "run_id": intent.run_id,
                "attempt_id": lease.attempt_id,
                "fencing_generation": lease.generation,
                "output_set_id": "set-" + uuid4().hex,
                "manifest_digest": "sha256:" + "d" * 64,
                "reservation_id": "res-"
                + hashlib.sha256(intent.run_id.encode()).hexdigest(),
                "operation_id": "publish-" + uuid4().hex,
                "issued_at": datetime.now(UTC).isoformat(),
            }
        )
        with self.assertRaises(CoordinationError):
            self.repository.reserve_finalization(lease, grant)

    def test_cancel_racing_reservation_has_one_serialized_policy_winner(self):
        intent, _ = self.create_run()
        lease = self.repository.claim(
            intent.tenant_id,
            intent.run_id,
            "fixture-worker",
            "token-race-" + intent.run_id,
            "claim-race-" + intent.run_id,
            30,
        )
        signer = Ed25519PublicationGrantSigner(b"z" * 32, key_id="integration-key")
        grant = signer.issue(
            {
                "tenant_id": intent.tenant_id,
                "run_id": intent.run_id,
                "attempt_id": lease.attempt_id,
                "fencing_generation": lease.generation,
                "output_set_id": "set-race-" + intent.run_id,
                "manifest_digest": "sha256:" + "f" * 64,
                "reservation_id": "res-"
                + hashlib.sha256(intent.run_id.encode()).hexdigest(),
                "operation_id": "publish-race-" + intent.run_id,
                "issued_at": datetime.now(UTC).isoformat(),
            }
        )
        barrier = threading.Barrier(2)

        def cancel():
            repo = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"])
            barrier.wait(timeout=5)
            return repo.cancel(
                intent.tenant_id, intent.run_id, "cancel-race-" + intent.run_id
            )

        def reserve():
            repo = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"])
            barrier.wait(timeout=5)
            try:
                return repo.reserve_finalization(lease, grant)
            except CoordinationError as error:
                return ("rejected", str(error))

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel_future = pool.submit(cancel)
            reserve_future = pool.submit(reserve)
            cancel_future.result(timeout=10)
            reserve_future.result(timeout=10)
        final = self.repository.get(intent.tenant_id, intent.run_id)
        if final.reservation is None:
            self.assertEqual(final.state, DurableRunState.CANCELLED)
        else:
            self.assertEqual(final.state, DurableRunState.FINALIZATION_PENDING)
            self.assertEqual(final.reservation.reservation_id, grant.reservation_id)
            self.assertIsNotNone(final.cancel_requested_at)

    def test_live_execution_binding_round_trips_through_durable_run(self):
        digest = "a" * 64
        binding = LiveExecutionBinding(
            source_id="controlled-http",
            source_revision_id="controlled-v1",
            policy_id="controlled-get",
            policy_version="1.0.0",
            policy_digest="sha256:" + "b" * 64,
            release_id="controlled-release-1",
            build_id=digest,
            build_manifest_digest="sha256:" + "c" * 64,
            runtime_profile_id="scrapy-live-http-v1",
            runtime_image_digest="sha256:" + "d" * 64,
            authority_id="integration-authority",
            authority_snapshot_generation=3,
            authority_snapshot_digest="sha256:" + "e" * 64,
            max_duration_seconds=60,
            max_requests=8,
            max_total_response_bytes=1_000_000,
            max_concurrency=1,
        )
        intent = DurableRunIntent(
            tenant_id="integration-tenant",
            run_id=str(uuid4()),
            request_id="live-binding-" + uuid4().hex,
            idempotency_key="live-binding-" + uuid4().hex,
            binding=binding,
            deadline=datetime.now(UTC) + timedelta(minutes=2),
        )
        self.repository.create(intent)
        reloaded = self.repository.get(intent.tenant_id, intent.run_id)
        self.assertEqual(reloaded.intent.binding, binding)
        self.assertEqual(reloaded.intent.binding.execution_mode, "live-authorized")

    def test_signed_authority_admission_and_current_attempt_capability(self):
        now = datetime.now(UTC)
        private_key = Ed25519PrivateKey.from_private_bytes(b"v" * 32)
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

        def record_digest(value: dict[str, object], field: str) -> str:
            unsigned = {key: item for key, item in value.items() if key != field}
            return "sha256:" + hashlib.sha256(rfc8785.dumps(unsigned)).hexdigest()

        source: dict[str, object] = {
            "source_id": "controlled-authority-http",
            "revision_id": "controlled-v1",
            "status": "eligible",
        }
        source["configuration_digest"] = record_digest(source, "configuration_digest")
        policy: dict[str, object] = {
            "policy_id": "controlled-authority-get",
            "policy_version": "1.0.0",
            "source_id": "controlled-authority-http",
            "source_revision_id": "controlled-v1",
            "status": "eligible",
            "policy_digest": "",
            "deadline_seconds": 60,
            "max_requests": 8,
            "max_total_response_bytes": 2_000_000,
            "max_concurrency": 1,
        }
        policy["policy_digest"] = record_digest(policy, "policy_digest")
        build_id = "sha256:" + "3" * 64
        build_manifest_digest = "sha256:" + "4" * 64
        runtime_image_digest = "sha256:" + "5" * 64
        release: dict[str, object] = {
            "contract_version": "bot-release/v2",
            "release_id": "controlled-authority-release",
            "status": "approved",
            "source_id": "controlled-authority-http",
            "source_revision_id": "controlled-v1",
            "policy_id": "controlled-authority-get",
            "policy_version": "1.0.0",
            "build_id": build_id,
            "build_manifest_digest": build_manifest_digest,
            "backend_id": "scrapy-live-http",
            "runtime_profile_id": "scrapy-live-http-v1",
            "runtime_image_digest": runtime_image_digest,
            "payload_contract": {
                "contract_id": "controlled.record",
                "version": "1.0.0",
                "digest": "sha256:" + "6" * 64,
            },
        }
        release["release_digest"] = record_digest(release, "release_digest")
        snapshot: dict[str, object] = {
            "contract_version": "live-authority-snapshot/v1",
            "authority_id": "controlled-authority-" + uuid4().hex,
            "generation": 4,
            "issued_at": (now - timedelta(seconds=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(),
            "source_revisions": [source],
            "policies": [policy],
            "releases": [release],
        }
        snapshot["snapshot_digest"] = record_digest(snapshot, "snapshot_digest")
        envelope = {
            "snapshot": snapshot,
            "key_id": "controlled-authority-test-key",
            "signature": base64.urlsafe_b64encode(
                private_key.sign(rfc8785.dumps(snapshot))
            )
            .decode("ascii")
            .rstrip("="),
        }
        authority = TrustedLiveAuthority(
            json.dumps(envelope, separators=(",", ":")).encode(),
            trusted_public_keys={"controlled-authority-test-key": public_key},
            minimum_generation=4,
            authority_registry=self.repository,
        )
        authority_id = str(snapshot["authority_id"])
        snapshot_digest = (
            "sha256:" + hashlib.sha256(rfc8785.dumps(snapshot)).hexdigest()
        )
        envelope_bytes = json.dumps(envelope, separators=(",", ":")).encode()
        envelope_digest = "sha256:" + hashlib.sha256(envelope_bytes).hexdigest()
        with self.assertRaisesRegex(CoordinationError, "rollback"):
            self.repository.install_authority_snapshot(
                authority_id=authority_id,
                generation=3,
                snapshot_digest="sha256:" + "1" * 64,
                envelope_digest="sha256:" + "2" * 64,
                signing_key_id="controlled-authority-test-key",
                expires_at=now + timedelta(minutes=5),
                signed_envelope=envelope,
            )
        with self.assertRaisesRegex(CoordinationError, "conflicts"):
            self.repository.install_authority_snapshot(
                authority_id=authority_id,
                generation=4,
                snapshot_digest="sha256:" + "1" * 64,
                envelope_digest="sha256:" + "2" * 64,
                signing_key_id="controlled-authority-test-key",
                expires_at=now + timedelta(minutes=5),
                signed_envelope=envelope,
            )
        self.assertTrue(
            self.repository.authority_snapshot_is_current(
                authority_id=authority_id,
                generation=4,
                snapshot_digest=snapshot_digest,
            )
        )
        self.assertTrue(
            self.repository.install_authority_snapshot(
                authority_id=authority_id,
                generation=4,
                snapshot_digest=snapshot_digest,
                envelope_digest=envelope_digest,
                signing_key_id="controlled-authority-test-key",
                expires_at=now + timedelta(minutes=10),
                signed_envelope=envelope,
            )
            == 4
        )
        capability_seed = b"w" * 32
        capability_signer = ExecutionCapabilitySigner(
            capability_seed, key_id="bc04-live-integration"
        )
        coordinator = DurableExecutionCoordinator(
            self.repository,
            publication=object(),
            grant_signer=None,
            live_authority=authority,
            execution_capability_signer=capability_signer,
            execution_capability_verifier=capability_signer,
        )
        run = coordinator.admit_live_run(
            tenant_id="integration-tenant",
            run_id=str(uuid4()),
            request_id="authorized-live-" + uuid4().hex,
            idempotency_key="authorized-live-" + uuid4().hex,
            deadline=now + timedelta(minutes=2),
            source_id="controlled-authority-http",
            source_revision_id="controlled-v1",
            policy_id="controlled-authority-get",
            policy_version="1.0.0",
            release_id="controlled-authority-release",
            build_id=build_id,
            build_manifest_digest=build_manifest_digest,
            runtime_image_digest=runtime_image_digest,
        )
        self.assertEqual(run.intent.binding.authority_snapshot_generation, 4)
        self.assertEqual(run.intent.binding.payload_contract_id, "controlled.record")
        self.assertEqual(
            run.intent.binding.payload_contract_digest, "sha256:" + "6" * 64
        )
        lease = coordinator.acquire_attempt(
            run.intent.tenant_id,
            run.intent.run_id,
            "live-worker",
            "claim-live-" + uuid4().hex,
        )
        capability = coordinator.issue_execution_capability(
            lease, audience="data-mozaic/acquisition-gateway"
        )
        verify_execution_capability(
            capability,
            trusted_public_keys={
                "bc04-live-integration": Ed25519PrivateKey.from_private_bytes(
                    capability_seed
                )
                .public_key()
                .public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            },
            expected_audience="data-mozaic/acquisition-gateway",
        )
        renewed = coordinator.renew_attempt(lease, "renew-live-" + uuid4().hex)
        with self.assertRaisesRegex(CoordinationError, "current attempt lease"):
            coordinator.issue_execution_capability(
                lease, audience="data-mozaic/acquisition-gateway"
            )
        replacement = coordinator.issue_execution_capability(
            renewed, audience="data-mozaic/acquisition-gateway"
        )
        self.assertNotEqual(capability["capability_id"], replacement["capability_id"])
        operation = coordinator.authorize_acquisition_operation(
            replacement,
            operation_id="gateway-op-" + uuid4().hex,
            target_digest="sha256:" + "6" * 64,
        )
        self.assertTrue(operation["operation_id"].startswith("gateway-op-"))
        with self.assertRaisesRegex(CoordinationError, "conflicts"):
            coordinator.authorize_acquisition_operation(
                replacement,
                operation_id=str(operation["operation_id"]),
                target_digest="sha256:" + "7" * 64,
            )
        coordinator.cancel(
            "integration-tenant",
            run.intent.run_id,
            "cancel-live-" + uuid4().hex,
        )
        with self.assertRaisesRegex(CoordinationError, "not current and authorized"):
            coordinator.authorize_acquisition_operation(
                replacement,
                operation_id="gateway-op-after-cancel-" + uuid4().hex,
                target_digest="sha256:" + "8" * 64,
            )

        revoked = json.loads(json.dumps(snapshot))
        revoked["generation"] = 5
        revoked["releases"][0]["status"] = "revoked"
        release = revoked["releases"][0]
        release.pop("release_digest")
        release["release_digest"] = (
            "sha256:" + hashlib.sha256(rfc8785.dumps(release)).hexdigest()
        )
        revoked.pop("snapshot_digest")
        revoked["snapshot_digest"] = (
            "sha256:" + hashlib.sha256(rfc8785.dumps(revoked)).hexdigest()
        )
        revoked_envelope = {
            "snapshot": revoked,
            "key_id": "controlled-authority-test-key",
            "signature": base64.urlsafe_b64encode(
                private_key.sign(rfc8785.dumps(revoked))
            )
            .decode("ascii")
            .rstrip("="),
        }
        revoked_authority = TrustedLiveAuthority(
            json.dumps(revoked_envelope, separators=(",", ":")).encode(),
            trusted_public_keys={"controlled-authority-test-key": public_key},
            minimum_generation=4,
            authority_registry=self.repository,
        )
        with self.assertRaisesRegex(CoordinationError, "stale or revoked"):
            authority.assert_durably_current()
        with self.assertRaisesRegex(CoordinationError, "not currently approved"):
            revoked_authority.resolve(
                source_id="controlled-authority-http",
                source_revision_id="controlled-v1",
                policy_id="controlled-authority-get",
                policy_version="1.0.0",
                release_id="controlled-authority-release",
                build_id=build_id,
                build_manifest_digest=build_manifest_digest,
                runtime_image_digest=runtime_image_digest,
            )


if __name__ == "__main__":
    unittest.main()
