from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from acquisition_application.application import (
    FixtureImportError,
    TrustedAcquisitionApplication,
)
from acquisition_application.context import (
    AcquisitionApplicationError,
    load_current_execution,
)
from acquisition_output.domain import PayloadContractRef
from execution_coordination.domain import ExecutionBinding
from execution_coordination.durable_domain import (
    DurableAttemptState,
    DurableRunIntent,
    DurableRunSnapshot,
    DurableRunState,
)


class _Reader:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get(self, tenant_id, run_id):
        if self.snapshot.intent.tenant_id != tenant_id:
            return None
        if self.snapshot.intent.run_id != run_id:
            return None
        return self.snapshot


class TrustedExecutionContextTests(unittest.TestCase):
    def setUp(self):
        now = datetime.now(UTC)
        self.tenant_id = "tenant-context-test"
        self.run_id = str(uuid4())
        binding = ExecutionBinding(
            source_id="synthetic-source",
            source_revision="revision-1",
            bot_id="bot-" + "a" * 64,
            build_id="b" * 64,
            bot_build_manifest_digest="c" * 64,
            assembly_id="d" * 64,
            catalog_digest="e" * 64,
            oci_image="registry.invalid/scrapy@sha256:" + "f" * 64,
            oci_digest="sha256:" + "f" * 64,
            local_image_id="sha256:" + "1" * 64,
            fixture_bundle_id="fixture-v1",
            fixture_bundle_digest="2" * 64,
            payload_contract_id="sample.record",
            payload_contract_version="1.0.0",
            payload_contract_digest="sha256:" + "3" * 64,
        )
        intent = DurableRunIntent(
            tenant_id=self.tenant_id,
            run_id=self.run_id,
            request_id="request-context-test",
            idempotency_key="context-test-idem",
            binding=binding,
            deadline=now + timedelta(minutes=2),
        )
        self.snapshot = DurableRunSnapshot(
            intent=intent,
            state=DurableRunState.RUNNING,
            generation=1,
            attempt_count=1,
            attempt_state=DurableAttemptState.RUNNING,
            lease_until=now + timedelta(minutes=1),
            reservation=None,
            publication_receipt=None,
            cancel_requested_at=None,
            terminal_reason=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
        )

    def test_context_is_resolved_from_current_bc04_state(self):
        context = load_current_execution(
            _Reader(self.snapshot), self.tenant_id, self.run_id
        )
        self.assertEqual(self.tenant_id, context.tenant_id)
        self.assertEqual(self.run_id, context.run_id)
        self.assertEqual(self.snapshot.attempt_id, context.attempt_id)
        self.assertEqual(1, context.generation)
        self.assertEqual("synthetic-source", context.source_id)
        self.assertEqual("fixture-only", context.execution_mode)
        self.assertEqual("sha256:" + "c" * 64, context.build_manifest_digest)
        self.assertEqual("sample.record", context.payload_contract.contract_id)

    def test_expired_and_terminal_attempts_cannot_import(self):
        now = datetime.now(UTC)
        for snapshot in (
            self._replace(lease_until=now - timedelta(seconds=1)),
            self._replace(state=DurableRunState.FAILED),
            self._replace(attempt_state=DurableAttemptState.EXPIRED),
        ):
            with self.assertRaises(AcquisitionApplicationError):
                load_current_execution(_Reader(snapshot), self.tenant_id, self.run_id)

    def test_tenant_and_run_are_not_caller_replaced(self):
        with self.assertRaises(AcquisitionApplicationError):
            load_current_execution(_Reader(self.snapshot), "other-tenant", self.run_id)
        with self.assertRaises(AcquisitionApplicationError):
            load_current_execution(_Reader(self.snapshot), self.tenant_id, str(uuid4()))

    def test_import_rejects_caller_contract_replacement_before_side_effects(self):
        app = TrustedAcquisitionApplication(_Reader(self.snapshot), object(), object())
        with self.assertRaisesRegex(FixtureImportError, "trusted execution binding"):
            app.import_verified_result(
                self.tenant_id,
                self.run_id,
                bundle=None,
                payload_contract=PayloadContractRef(
                    "sample.record", "1.0.0", "sha256:" + "4" * 64
                ),
                producer=None,
            )

    def test_completed_replay_requires_exact_bc04_publication_receipt(self):
        reservation = SimpleNamespace(
            tenant_id=self.tenant_id,
            run_id=self.run_id,
            attempt_id=self.snapshot.attempt_id,
            fencing_generation=1,
            output_set_id="set-verified",
            manifest_digest="sha256:" + "a" * 64,
            reservation_id="reservation-verified",
        )
        receipt = {
            "tenant_id": self.tenant_id,
            "output_set_id": reservation.output_set_id,
            "manifest_digest": reservation.manifest_digest,
            "finalization_reservation_id": reservation.reservation_id,
        }
        completed = self._replace(
            state=DurableRunState.SUCCEEDED,
            attempt_state=DurableAttemptState.SUCCEEDED,
            lease_until=None,
            reservation=reservation,
            publication_receipt=receipt,
        )
        context = load_current_execution(
            _Reader(completed),
            self.tenant_id,
            self.run_id,
            allow_published_replay=True,
        )
        self.assertEqual(reservation.output_set_id, context.replay_output_set_id)
        self.assertEqual(reservation.manifest_digest, context.replay_manifest_digest)
        with self.assertRaises(AcquisitionApplicationError):
            load_current_execution(
                _Reader(
                    self._replace(
                        state=DurableRunState.SUCCEEDED,
                        attempt_state=DurableAttemptState.SUCCEEDED,
                        lease_until=None,
                        reservation=reservation,
                        publication_receipt={},
                    )
                ),
                self.tenant_id,
                self.run_id,
                allow_published_replay=True,
            )

    def _replace(self, **changes):
        values = {
            name: getattr(self.snapshot, name)
            for name in self.snapshot.__dataclass_fields__
        }
        values.update(changes)
        return DurableRunSnapshot(**values)


if __name__ == "__main__":
    unittest.main()
