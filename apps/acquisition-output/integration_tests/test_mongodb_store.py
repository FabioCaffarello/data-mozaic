from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from acquisition_output.adapters.mongodb import (
    MongoOutputStore,
    MongoResources,
    MongoSettings,
    mapper,
)
from acquisition_output.adapters.mongodb.config import MongoProvisioningSettings
from acquisition_output.adapters.mongodb.errors import MongoProvisioningDrift
from acquisition_output.adapters.mongodb.provision import provision
from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import (
    InvalidTransition,
    OutputConflict,
    OutputIdentity,
    OutputSetBinding,
    OutputSetSpec,
    OutputSetState,
    PayloadContractRef,
    ProducerIdentity,
    RecordOutput,
    ScopedOperationIdentity,
    SourceRevisionRef,
    canonical_digest,
    output_intent_digest,
)
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, WriteError


class _Contracts:
    def validate(self, contract, payload):
        if contract.contract_id != "synthetic.mongo.v1" or set(payload) != {
            "ordinal",
            "label",
        }:
            raise ValueError("unexpected integration fixture contract")


class _NoCustody:
    def confirm(self, artifact, *, tenant_id, source):
        raise AssertionError("record integration case must not call artifact custody")


def _app(store):
    return AcquisitionOutputApplication(
        store, store, payload_contracts=_Contracts(), custody=_NoCustody(), outbox=store
    )


def _fixture(tenant: str, set_id: str, ordinal: int, *, observation: str | None = None):
    contract = PayloadContractRef(
        "synthetic.mongo.v1",
        "1.0.0",
        canonical_digest({"contract": "synthetic.mongo.v1"}),
    )
    binding = OutputSetBinding(
        tenant,
        SourceRevisionRef("fixture-source", "synthetic-2026-11"),
        ProducerIdentity("integration-test", "1"),
    )
    output_id = f"output-{ordinal:06d}"
    return (
        contract,
        binding,
        RecordOutput(
            identity=OutputIdentity(
                output_id,
                observation or f"observation-{ordinal:06d}",
                datetime.now(UTC),
            ),
            operation=ScopedOperationIdentity(tenant, f"stage-{set_id}-{ordinal:06d}"),
            output_set_id=set_id,
            binding=binding,
            payload={"ordinal": ordinal, "label": f"synthetic-{ordinal}"},
            payload_contract=contract,
        ),
    )


class MongoOutputStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = MongoSettings.from_env()
        with MongoResources(cls.settings) as resources:
            resources.client.admin.command("ping")
            provision(resources.database)

    def setUp(self):
        self.tenant = "it-" + uuid4().hex
        self.set_id = "set-" + uuid4().hex
        self.resource = MongoResources(self.settings)
        self.resource.__enter__()
        self.store = MongoOutputStore(self.resource)
        contract, binding, _ = _fixture(self.tenant, self.set_id, 1)
        self.contract = contract
        self.binding = binding
        self.app = _app(self.store)
        self.app.create_output_set(
            OutputSetSpec(self.set_id, binding, (contract,)),
            ScopedOperationIdentity(self.tenant, f"create-{self.set_id}"),
        )

    def tearDown(self):
        self.resource.__exit__(None, None, None)

    def _output(self, ordinal: int):
        return _fixture(self.tenant, self.set_id, ordinal)[2]

    def test_restart_replay_manifest_publication_outbox_and_published_queries(self):
        output = self._output(1)
        receipt = self.app.stage_output(output)
        self.resource.__exit__(None, None, None)
        self.resource = MongoResources(self.settings)
        self.resource.__enter__()
        self.store = MongoOutputStore(self.resource)
        self.app = _app(self.store)
        self.assertEqual(self.app.stage_output(output), receipt)
        seal_operation = ScopedOperationIdentity(self.tenant, "seal")
        sealed = self.app.seal_output_set(self.tenant, self.set_id, seal_operation)
        self.assertEqual(sealed.member_count, 1)
        published = self.app.publish_output_set(
            self.tenant,
            self.set_id,
            sealed.manifest_digest,
            ScopedOperationIdentity(self.tenant, "publish"),
        )
        self.assertEqual(
            self.app.get_publication_receipt(self.tenant, self.set_id), published
        )
        self.assertEqual(
            self.app.seal_output_set(self.tenant, self.set_id, seal_operation), sealed
        )
        events = self.app.list_pending_events(self.tenant)
        self.assertEqual(
            len([event for event in events if event["aggregate_id"] == self.set_id]), 1
        )
        page = self.app.read_published_outputs(self.tenant, self.set_id, limit=1)
        self.assertEqual(
            tuple(item.identity.output_id for item in page.outputs),
            (output.identity.output_id,),
        )

    def test_changed_operation_intent_conflicts_across_clients(self):
        first = self._output(1)
        self.app.stage_output(first)
        changed = _fixture(self.tenant, self.set_id, 2)[2]
        changed = RecordOutput(
            identity=OutputIdentity(
                first.identity.output_id,
                first.identity.observation_id,
                first.identity.observed_at,
            ),
            operation=first.operation,
            output_set_id=self.set_id,
            binding=self.binding,
            payload={"ordinal": 999, "label": "different"},
            payload_contract=self.contract,
        )
        with self.assertRaises(OutputConflict):
            self.app.stage_output(changed)

    def test_independent_client_stage_stage(self):
        barrier = threading.Barrier(2)
        second_resource = MongoResources(self.settings)
        second_resource.__enter__()
        second_store = MongoOutputStore(second_resource)
        second_app = _app(second_store)

        def stage(app, output):
            barrier.wait()
            return app.stage_output(output)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(stage, self.app, self._output(1))
                second = pool.submit(stage, second_app, self._output(2))
                first.result(timeout=30)
                second.result(timeout=30)
        finally:
            second_resource.__exit__(None, None, None)
        self.assertEqual(
            self.app.inspect_output_set(self.tenant, self.set_id).member_count, 2
        )

    def test_same_operation_replay_race_returns_one_durable_receipt(self):
        output = self._output(1)
        barrier = threading.Barrier(2)
        second_resource = MongoResources(self.settings)
        second_resource.__enter__()
        second_app = _app(MongoOutputStore(second_resource))

        def stage(app):
            barrier.wait()
            return app.stage_output(output)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(stage, self.app)
                second = pool.submit(stage, second_app)
                self.assertEqual(first.result(timeout=30), second.result(timeout=30))
        finally:
            second_resource.__exit__(None, None, None)
        self.assertEqual(
            self.app.inspect_output_set(self.tenant, self.set_id).member_count, 1
        )

    def test_independent_client_seal_seal(self):
        self.app.stage_output(self._output(1))
        barrier = threading.Barrier(2)
        second_resource = MongoResources(self.settings)
        second_resource.__enter__()
        second_app = _app(MongoOutputStore(second_resource))

        def seal(app, op):
            barrier.wait()
            return app.seal_output_set(
                self.tenant, self.set_id, ScopedOperationIdentity(self.tenant, op)
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(seal, self.app, "seal-a")
                second = pool.submit(seal, second_app, "seal-b")
                first_result = first.result(timeout=30)
                second_result = second.result(timeout=30)
        finally:
            second_resource.__exit__(None, None, None)
        self.assertEqual(first_result.manifest_digest, second_result.manifest_digest)
        self.assertEqual(
            self.app.inspect_output_set(self.tenant, self.set_id).state,
            OutputSetState.SEALED,
        )

    def test_independent_client_stage_vs_seal(self):
        scan_started = threading.Event()
        allow_scan = threading.Event()
        second_resource = MongoResources(self.settings)
        second_resource.__enter__()
        second_store = MongoOutputStore(second_resource)
        second_app = _app(second_store)
        original_manifest_scan = second_store._manifest_digest

        def delayed_manifest_scan(snapshot):
            scan_started.set()
            if not allow_scan.wait(timeout=10):
                raise TimeoutError("stage/seal test did not release manifest scan")
            return original_manifest_scan(snapshot)

        second_store._manifest_digest = delayed_manifest_scan

        def seal():
            return second_app.seal_output_set(
                self.tenant,
                self.set_id,
                ScopedOperationIdentity(self.tenant, "racing-seal"),
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                sealed = pool.submit(seal)
                self.assertTrue(scan_started.wait(timeout=10))
                stage_result = self.app.stage_output(self._output(1))
                allow_scan.set()
                manifest = sealed.result(timeout=30)
        finally:
            allow_scan.set()
            second_resource.__exit__(None, None, None)
        self.assertEqual(manifest.state, OutputSetState.SEALED)
        self.assertEqual(manifest.member_count, 1)
        self.assertEqual(stage_result.output_set_id, self.set_id)

    def test_publish_race_is_idempotent_and_writes_one_outbox_fact(self):
        self.app.stage_output(self._output(1))
        sealed = self.app.seal_output_set(
            self.tenant, self.set_id, ScopedOperationIdentity(self.tenant, "seal")
        )
        barrier = threading.Barrier(2)
        second_resource = MongoResources(self.settings)
        second_resource.__enter__()
        second_app = _app(MongoOutputStore(second_resource))

        def publish(app, op):
            barrier.wait()
            return app.publish_output_set(
                self.tenant,
                self.set_id,
                sealed.manifest_digest,
                ScopedOperationIdentity(self.tenant, op),
            )

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(publish, self.app, "publish-a")
                second = pool.submit(publish, second_app, "publish-b")
                first_result = first.result(timeout=30)
                second_result = second.result(timeout=30)
        finally:
            second_resource.__exit__(None, None, None)
        self.assertEqual(first_result.publication_id, second_result.publication_id)
        events = [
            event
            for event in self.app.list_pending_events(self.tenant)
            if event["aggregate_id"] == self.set_id
        ]
        self.assertEqual(len(events), 1)

    def test_publish_vs_abort_has_one_terminal_winner(self):
        self.app.stage_output(self._output(1))
        sealed = self.app.seal_output_set(
            self.tenant, self.set_id, ScopedOperationIdentity(self.tenant, "seal")
        )
        barrier = threading.Barrier(2)
        second_resource = MongoResources(self.settings)
        second_resource.__enter__()
        second_app = _app(MongoOutputStore(second_resource))

        def publish():
            barrier.wait()
            try:
                return self.app.publish_output_set(
                    self.tenant,
                    self.set_id,
                    sealed.manifest_digest,
                    ScopedOperationIdentity(self.tenant, "publish-race"),
                )
            except InvalidTransition as error:
                return error

        def abort():
            barrier.wait()
            try:
                return second_app.abort_output_set(
                    self.tenant,
                    self.set_id,
                    ScopedOperationIdentity(self.tenant, "abort-race"),
                    "concurrent fixture abort",
                )
            except InvalidTransition as error:
                return error

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                published = pool.submit(publish)
                aborted = pool.submit(abort)
                publish_result = published.result(timeout=30)
                abort_result = aborted.result(timeout=30)
        finally:
            second_resource.__exit__(None, None, None)
        state = self.app.inspect_output_set(self.tenant, self.set_id).state
        self.assertIn(state, {OutputSetState.PUBLISHED, OutputSetState.ABORTED})
        if state == OutputSetState.PUBLISHED:
            self.assertNotIsInstance(publish_result, InvalidTransition)
            self.assertIsInstance(abort_result, InvalidTransition)
        else:
            self.assertIsInstance(publish_result, InvalidTransition)
            self.assertNotIsInstance(abort_result, InvalidTransition)

    def test_lost_ack_after_commit_is_resolved_by_operation_replay(self):
        self.app.stage_output(self._output(1))
        sealed = self.app.seal_output_set(
            self.tenant, self.set_id, ScopedOperationIdentity(self.tenant, "seal")
        )
        operation = ScopedOperationIdentity(self.tenant, "publish-lost-ack")
        original_transaction = self.store._transaction

        def commit_then_lose_ack(callback):
            original_transaction(callback)
            raise TimeoutError("simulated response loss after MongoDB commit")

        self.store._transaction = commit_then_lose_ack
        try:
            with self.assertRaises(TimeoutError):
                self.app.publish_output_set(
                    self.tenant, self.set_id, sealed.manifest_digest, operation
                )
        finally:
            self.store._transaction = original_transaction
        recovered = self.app.publish_output_set(
            self.tenant, self.set_id, sealed.manifest_digest, operation
        )
        self.assertEqual(
            recovered,
            self.app.get_publication_receipt(self.tenant, self.set_id),
        )

    def test_validator_rejects_invalid_storage_document(self):
        with self.assertRaises(WriteError):
            self.store._outputs.insert_one({"_id": "bad", "tenant_id": self.tenant})

    def test_unique_output_identity_index_is_enforced(self):
        output = self._output(1)
        self.app.stage_output(output)
        duplicate = mapper.output_document(output, output_intent_digest(output), ())
        duplicate["_id"] = "different-physical-document-id"
        with self.assertRaises(DuplicateKeyError):
            self.store._outputs.insert_one(duplicate)

    def test_stage_transaction_rolls_back_member_and_operation_receipt(self):
        output = self._output(1)
        collection = self.store._outputs

        class FailAfterInsert:
            def __getattr__(self, name):
                return getattr(collection, name)

            def insert_one(self, document, *, session):
                collection.insert_one(document, session=session)
                raise RuntimeError("injected failure after member insert")

        self.store._outputs = FailAfterInsert()
        with self.assertRaises(RuntimeError):
            self.app.stage_output(output)
        self.store._outputs = collection
        self.assertIsNone(
            collection.find_one(
                {
                    "tenant_id": self.tenant,
                    "output_set_id": self.set_id,
                    "output_id": output.identity.output_id,
                }
            )
        )
        self.assertIsNone(
            self.store._operations.find_one(
                {
                    "tenant_id": self.tenant,
                    "operation_id": output.operation.operation_id,
                }
            )
        )
        self.assertEqual(
            self.app.inspect_output_set(self.tenant, self.set_id).member_count, 0
        )

    def test_publication_transaction_rolls_back_set_receipt_and_outbox(self):
        self.app.stage_output(self._output(1))
        sealed = self.app.seal_output_set(
            self.tenant, self.set_id, ScopedOperationIdentity(self.tenant, "seal")
        )
        collection = self.store._outbox

        class FailAfterInsert:
            def __getattr__(self, name):
                return getattr(collection, name)

            def insert_one(self, document, *, session):
                collection.insert_one(document, session=session)
                raise RuntimeError("injected failure after outbox insert")

        self.store._outbox = FailAfterInsert()
        with self.assertRaises(RuntimeError):
            self.app.publish_output_set(
                self.tenant,
                self.set_id,
                sealed.manifest_digest,
                ScopedOperationIdentity(self.tenant, "publish-rollback"),
            )
        self.store._outbox = collection
        self.assertEqual(
            self.app.inspect_output_set(self.tenant, self.set_id).state,
            OutputSetState.SEALED,
        )
        self.assertIsNone(self.app.get_publication_receipt(self.tenant, self.set_id))
        self.assertEqual(
            collection.count_documents(
                {"tenant_id": self.tenant, "aggregate_id": self.set_id}
            ),
            0,
        )

    def test_provisioning_detects_validator_drift_without_dropping_data(self):
        admin = MongoProvisioningSettings.from_env()
        with MongoClient(admin.uri, serverSelectionTimeoutMS=5000) as client:
            database = client[admin.database]
            original_count = database.output_sets.count_documents(
                {"tenant_id": self.tenant}
            )
            database.command(
                "collMod",
                "output_sets",
                validator={"$jsonSchema": {"bsonType": "object", "required": ["_id"]}},
                validationLevel="strict",
                validationAction="error",
            )
            try:
                with self.assertRaises(MongoProvisioningDrift):
                    provision(database)
            finally:
                from acquisition_output.adapters.mongodb.provision import COLLECTIONS

                database.command(
                    "collMod",
                    "output_sets",
                    validator={"$jsonSchema": COLLECTIONS["output_sets"]},
                    validationLevel="strict",
                    validationAction="error",
                )
            self.assertEqual(
                database.output_sets.count_documents({"tenant_id": self.tenant}),
                original_count,
            )

    def test_sealed_set_is_not_visible_and_abort_wins_visibility_gate(self):
        output = self._output(1)
        self.app.stage_output(output)
        self.app.seal_output_set(
            self.tenant, self.set_id, ScopedOperationIdentity(self.tenant, "seal")
        )
        self.assertEqual(
            self.app.read_published_outputs(self.tenant, self.set_id).outputs, ()
        )
        with self.assertRaises(InvalidTransition):
            self.app.stage_output(
                replace(
                    output,
                    operation=ScopedOperationIdentity(self.tenant, "late-stage"),
                )
            )
        self.app.abort_output_set(
            self.tenant,
            self.set_id,
            ScopedOperationIdentity(self.tenant, "abort"),
            "fixture test",
        )
        self.assertEqual(
            self.app.read_published_outputs(self.tenant, self.set_id).outputs, ()
        )

    def test_published_keyset_pages_are_bounded_and_query_scoped(self):
        for ordinal in range(3):
            self.app.stage_output(self._output(ordinal))
        sealed = self.app.seal_output_set(
            self.tenant, self.set_id, ScopedOperationIdentity(self.tenant, "seal")
        )
        self.app.publish_output_set(
            self.tenant,
            self.set_id,
            sealed.manifest_digest,
            ScopedOperationIdentity(self.tenant, "publish"),
        )
        first = self.app.read_published_outputs(self.tenant, self.set_id, limit=1)
        second = self.app.read_published_outputs(
            self.tenant, self.set_id, cursor=first.next_cursor, limit=1
        )
        third = self.app.read_published_outputs(
            self.tenant, self.set_id, cursor=second.next_cursor, limit=1
        )
        self.assertEqual(
            [page.outputs[0].identity.output_id for page in (first, second, third)],
            ["output-000000", "output-000001", "output-000002"],
        )
        self.assertIsNone(third.next_cursor)
        with self.assertRaises(ValueError):
            self.app.read_published_outputs(
                "other-tenant", self.set_id, cursor=first.next_cursor
            )
        self.assertEqual(self.app.list_pending_events("other-tenant"), ())


if __name__ == "__main__":
    unittest.main()
