from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import (
    ArtifactItem,
    ArtifactOutput,
    ArtifactRef,
    CustodyReceipt,
    InvalidTransition,
    OutputConflict,
    OutputError,
    OutputIdentity,
    OutputSetState,
    PayloadContractRef,
    RecordOutput,
    ScopedOperationIdentity,
)
from acquisition_output.memory import InMemoryOutputStore
from test_domain import (
    ARTIFACT_DIGEST,
    CONTRACT,
    OBSERVED_AT,
    binding,
    record,
    set_spec,
)


class ExactFixtureContractValidator:
    """Independent acceptance rules for the synthetic example contract."""

    def __init__(self) -> None:
        self.calls = 0

    def validate(self, contract: PayloadContractRef, payload):
        self.calls += 1
        if contract != CONTRACT:
            raise OutputError("unknown fixture payload contract")
        if set(payload) != {"name", "price", "active"}:
            raise OutputError(
                "fixture payload must contain exactly name, price, active"
            )
        if (
            not isinstance(payload["name"], str)
            or not isinstance(payload["price"], (int, float))
            or not isinstance(payload["active"], bool)
        ):
            raise OutputError("fixture payload has invalid field types")


class RecordingCustody:
    def __init__(self, *, mismatch: bool = False) -> None:
        self.calls = 0
        self.mismatch = mismatch

    def confirm(self, artifact: ArtifactRef, *, tenant_id: str, source):
        self.calls += 1
        return CustodyReceipt(
            tenant_id if not self.mismatch else "other-tenant",
            artifact.artifact_id,
            artifact.digest,
            artifact.size_bytes,
            f"custody-{self.calls}",
        )


class AcquisitionOutputApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryOutputStore()
        self.validator = ExactFixtureContractValidator()
        self.custody = RecordingCustody()
        self.app = AcquisitionOutputApplication(
            self.store,
            self.store,
            payload_contracts=self.validator,
            custody=self.custody,
        )
        self.app.create_output_set(
            set_spec(), ScopedOperationIdentity("tenant-a", "create-a")
        )

    def test_create_operation_replay_and_changed_intent(self):
        first = self.app.create_output_set(
            set_spec(), ScopedOperationIdentity("tenant-a", "create-a")
        )
        self.assertEqual(first.output_set_id, "set-a")
        with self.assertRaises(OutputConflict):
            self.app.create_output_set(
                set_spec(expected=1), ScopedOperationIdentity("tenant-a", "create-a")
            )
        independent = self.app.create_output_set(
            set_spec(set_id="set-a", tenant="another-tenant"),
            ScopedOperationIdentity("another-tenant", "create-other"),
        )
        self.assertEqual(independent.binding.tenant_id, "another-tenant")
        with self.assertRaises(OutputError):
            self.app.create_output_set(
                set_spec(set_id="different-set"),
                ScopedOperationIdentity("another-tenant", "wrong-operation-tenant"),
            )

    def test_stage_and_exact_retry_return_same_receipt_without_rechecking_custody(self):
        output = record()
        first = self.app.stage_output(output)
        again = self.app.stage_output(output)
        self.assertEqual(first, again)
        self.assertEqual(self.validator.calls, 1)
        self.assertEqual(self.custody.calls, 0)

    def test_same_operation_identity_with_changed_intent_conflicts(self):
        first = record()
        self.app.stage_output(first)
        changed = record(payload={"name": "Changed", "price": 12.5, "active": True})
        with self.assertRaises(OutputConflict):
            self.app.stage_output(changed)

    def test_same_payload_with_new_observation_and_operation_stays_distinct(self):
        first = record()
        second = record(
            output_id="output-b", observation_id="observation-b", operation_id="stage-b"
        )
        first_receipt = self.app.stage_output(first)
        second_receipt = self.app.stage_output(second)
        self.assertNotEqual(first_receipt.output_id, second_receipt.output_id)
        self.assertNotEqual(first_receipt.intent_digest, second_receipt.intent_digest)
        manifest = self.app.seal_output_set(
            "tenant-a", "set-a", ScopedOperationIdentity("tenant-a", "seal-a")
        )
        self.assertEqual(manifest.member_count, 2)
        self.assertFalse(hasattr(manifest, "members"))

    def test_output_set_operations_return_bounded_snapshot_without_members(self):
        self.app.stage_output(record())
        snapshot = self.app.inspect_output_set("tenant-a", "set-a")
        self.assertEqual(snapshot.member_count, 1)
        self.assertFalse(hasattr(snapshot, "members"))

    def test_contract_validator_rejects_arbitrary_storage_configuration_payload(self):
        invalid = record(
            payload={
                "name": "Synthetic",
                "price": 1,
                "active": True,
                "database": "mongo",
            }
        )
        with self.assertRaises(OutputError):
            self.app.stage_output(invalid)

    def test_output_cannot_cross_tenant_source_or_attempt_binding(self):
        with self.assertRaises(OutputError):
            self.app.stage_output(record(tenant="another-tenant"))
        with self.assertRaises(OutputConflict):
            self.app.stage_output(record(source_id="another-source"))
        set_with_run = self.store.create(
            set_spec(set_id="bound-run", expected=1),
            ScopedOperationIdentity("tenant-a", "create-bound-run"),
        )
        self.assertEqual(set_with_run.output_set_id, "bound-run")
        wrong_attempt = RecordOutput(
            identity=OutputIdentity("wrong-attempt-output", "observation", OBSERVED_AT),
            operation=ScopedOperationIdentity("tenant-a", "wrong-attempt-op"),
            output_set_id="bound-run",
            binding=binding(run="run-other"),
            payload_contract=CONTRACT,
            payload={"name": "Synthetic", "price": 1, "active": True},
        )
        with self.assertRaises(OutputConflict):
            self.app.stage_output(wrong_attempt)

    def test_artifact_output_requires_matching_custody_receipt(self):
        artifact = ArtifactRef("artifact-a", ARTIFACT_DIGEST, 24, "application/pdf")
        output = ArtifactOutput(
            identity=OutputIdentity(
                "artifact-output", "observation-artifact", OBSERVED_AT
            ),
            operation=ScopedOperationIdentity("tenant-a", "stage-artifact"),
            output_set_id="set-a",
            binding=binding(),
            artifacts=(ArtifactItem(artifact),),
        )
        self.app.stage_output(output)
        self.assertEqual(self.custody.calls, 1)
        manifest = self.app.inspect_output_set("tenant-a", "set-a")
        self.assertEqual(manifest.artifact_reference_count, 1)
        self.assertEqual(manifest.custody_reference_count, 1)

    def test_mismatched_custody_receipt_prevents_staging(self):
        bad_app = AcquisitionOutputApplication(
            self.store,
            self.store,
            payload_contracts=self.validator,
            custody=RecordingCustody(mismatch=True),
        )
        artifact = ArtifactRef("artifact-b", ARTIFACT_DIGEST, 24)
        output = ArtifactOutput(
            identity=OutputIdentity(
                "artifact-output-b", "observation-artifact-b", OBSERVED_AT
            ),
            operation=ScopedOperationIdentity("tenant-a", "stage-artifact-b"),
            output_set_id="set-a",
            binding=binding(),
            artifacts=(ArtifactItem(artifact),),
        )
        with self.assertRaises(OutputError):
            bad_app.stage_output(output)
        manifest = self.app.inspect_output_set("tenant-a", "set-a")
        self.assertEqual(manifest.member_count, 0)

    def test_stage_seal_publish_visibility_and_idempotent_receipts(self):
        first = record()
        second = record(
            output_id="output-b", observation_id="observation-b", operation_id="stage-b"
        )
        self.app.stage_output(first)
        self.app.stage_output(second)
        self.assertEqual(self.app.read_published_outputs("tenant-a", "set-a"), ())
        seal_op = ScopedOperationIdentity("tenant-a", "seal-a")
        sealed = self.app.seal_output_set("tenant-a", "set-a", seal_op)
        self.assertEqual(sealed.state, OutputSetState.SEALED)
        self.assertEqual(self.app.seal_output_set("tenant-a", "set-a", seal_op), sealed)
        self.assertTrue(sealed.manifest_digest.startswith("sha256:"))
        with self.assertRaises(InvalidTransition):
            self.app.stage_output(
                record(output_id="late", observation_id="late", operation_id="late")
            )
        publish_op = ScopedOperationIdentity("tenant-a", "publish-a")
        receipt = self.app.publish_output_set(
            "tenant-a", "set-a", sealed.manifest_digest, publish_op
        )
        self.assertEqual(
            receipt,
            self.app.publish_output_set(
                "tenant-a", "set-a", sealed.manifest_digest, publish_op
            ),
        )
        published = self.app.inspect_output_set("tenant-a", "set-a")
        self.assertEqual(published.state, OutputSetState.PUBLISHED)
        self.assertEqual(
            len(self.app.read_published_outputs("tenant-a", "set-a", limit=1)), 1
        )
        first_page = self.app.read_published_outputs("tenant-a", "set-a", limit=1)
        second_page = self.app.read_published_outputs(
            "tenant-a",
            "set-a",
            after_output_id=first_page[-1].identity.output_id,
            limit=1,
        )
        self.assertEqual(len(second_page), 1)
        self.assertNotEqual(
            first_page[0].identity.output_id, second_page[0].identity.output_id
        )
        with self.assertRaises(InvalidTransition):
            self.app.abort_output_set(
                "tenant-a",
                "set-a",
                ScopedOperationIdentity("tenant-a", "abort-late"),
                "too late",
            )

    def test_publish_rejects_wrong_manifest_and_conflicting_operation_reuse(self):
        self.app.stage_output(record())
        sealed = self.app.seal_output_set(
            "tenant-a", "set-a", ScopedOperationIdentity("tenant-a", "seal-b")
        )
        with self.assertRaises(OutputConflict):
            self.app.publish_output_set(
                "tenant-a",
                "set-a",
                "sha256:" + "0" * 64,
                ScopedOperationIdentity("tenant-a", "publish-b"),
            )
        receipt = self.app.publish_output_set(
            "tenant-a",
            "set-a",
            sealed.manifest_digest,
            ScopedOperationIdentity("tenant-a", "publish-b"),
        )
        self.assertEqual(
            self.app.publish_output_set(
                "tenant-a",
                "set-a",
                sealed.manifest_digest,
                ScopedOperationIdentity("tenant-a", "publish-b"),
            ),
            receipt,
        )
        with self.assertRaises(OutputConflict):
            self.app.publish_output_set(
                "tenant-a",
                "set-a",
                "sha256:" + "0" * 64,
                ScopedOperationIdentity("tenant-a", "publish-b"),
            )

    def test_abort_and_reject_are_terminal_and_not_visible(self):
        aborted = self.app.abort_output_set(
            "tenant-a",
            "set-a",
            ScopedOperationIdentity("tenant-a", "abort-a"),
            "fixture cancelled",
        )
        self.assertEqual(aborted.state, OutputSetState.ABORTED)
        self.assertEqual(self.app.read_published_outputs("tenant-a", "set-a"), ())
        other = self.store.create(
            set_spec(set_id="set-rejected"),
            ScopedOperationIdentity("tenant-a", "create-rejected"),
        )
        self.assertEqual(other.state, OutputSetState.STAGING)
        rejected = self.app.reject_output_set(
            "tenant-a",
            "set-rejected",
            ScopedOperationIdentity("tenant-a", "reject-a"),
            "contract violation",
        )
        self.assertEqual(rejected.state, OutputSetState.REJECTED)
        self.assertEqual(
            self.app.read_published_outputs("tenant-a", "set-rejected"), ()
        )

    def test_page_size_and_tenant_filter_are_enforced(self):
        self.assertIsNone(self.app.inspect_output_set("other-tenant", "set-a"))
        with self.assertRaises(OutputError):
            self.app.read_published_outputs("tenant-a", "set-a", limit=1001)

    def test_in_memory_reference_adapter_has_global_set_and_byte_limits(self):
        bounded = InMemoryOutputStore(max_sets=1, max_stored_bytes=100)
        bounded.create(
            set_spec(), ScopedOperationIdentity("tenant-a", "create-limited")
        )
        with self.assertRaises(OutputError):
            bounded.create(
                set_spec(set_id="second-set"),
                ScopedOperationIdentity("tenant-a", "create-over-limit"),
            )
        limited_app = AcquisitionOutputApplication(
            bounded,
            bounded,
            payload_contracts=self.validator,
            custody=self.custody,
        )
        with self.assertRaises(OutputError):
            limited_app.stage_output(record())

    def test_concurrent_stage_and_seal_have_one_local_linearization_order(self):
        barrier = threading.Barrier(2)
        output = record()

        def stage():
            barrier.wait()
            try:
                return self.app.stage_output(output)
            except InvalidTransition as error:
                return error

        def seal():
            barrier.wait()
            return self.app.seal_output_set(
                "tenant-a", "set-a", ScopedOperationIdentity("tenant-a", "racing-seal")
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            staged = pool.submit(stage)
            sealed = pool.submit(seal)
            stage_result = staged.result()
            manifest = sealed.result()
        self.assertIn(manifest.state, {OutputSetState.SEALED, OutputSetState.PUBLISHED})
        if isinstance(stage_result, InvalidTransition):
            self.assertEqual(manifest.member_count, 0)
        else:
            self.assertEqual(manifest.member_count, 1)
        self.assertEqual(self.app.read_published_outputs("tenant-a", "set-a"), ())


if __name__ == "__main__":
    unittest.main()
