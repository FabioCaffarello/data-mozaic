from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from mozaic_contract_resolution import ContractReference
from work_intake.adapters.postgres import PostgresWorkIntakeStore
from work_intake.domain import (
    DispatchState,
    LogicalInput,
    SubmissionIntent,
    WorkIntakeError,
    canonical_parameters,
)

ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(
    os.environ.get("WORK_INTAKE_TEST_DATABASE_URL"),
    "WORK_INTAKE_TEST_DATABASE_URL is not configured",
)
class PostgresWorkIntakeStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = PostgresWorkIntakeStore(
            os.environ["WORK_INTAKE_TEST_DATABASE_URL"], ROOT / "migrations"
        )

    def setUp(self):
        suffix = uuid.uuid4().hex
        self.tenant_id = "integration-" + suffix
        self.input = LogicalInput(
            self.tenant_id,
            "input-" + suffix,
            "controlled-http",
            "controlled-v1",
            ContractReference("controlled-input", "1.0.0", "sha256:" + "a" * 64),
        )
        parameters, digest = canonical_parameters({"page": 1})
        self.intent = SubmissionIntent(
            tenant_id=self.tenant_id,
            submission_id=str(uuid.uuid4()),
            input_id=self.input.input_id,
            source_id=self.input.source_id,
            source_revision_id=self.input.source_revision_id,
            idempotency_scope="integration-client",
            idempotency_key=suffix,
            release_id="controlled-release-v1",
            input_contract=self.input.contract,
            parameters_json=parameters,
            parameters_digest=digest,
            created_at=datetime.now(UTC),
            dispatch_operation_id="dispatch-" + suffix,
        )

    def test_runtime_role_cannot_mutate_accepted_submission_intent(self):
        with psycopg.connect(os.environ["WORK_INTAKE_TEST_DATABASE_URL"]) as connection:
            row = connection.execute(
                """SELECT has_column_privilege(current_user,
                       'work_intake.submissions','release_id','UPDATE'),
                          has_column_privilege(current_user,
                       'work_intake.submissions','parameters_json','UPDATE'),
                          has_column_privilege(current_user,
                       'work_intake.submissions','dispatch_state','UPDATE')"""
            ).fetchone()
        self.assertEqual((False, False, True), row)

    def test_acceptance_survives_a_fresh_store_and_identical_replay(self):
        first = self.store.accept(self.input, self.intent)
        fresh_store = PostgresWorkIntakeStore(
            os.environ["WORK_INTAKE_TEST_DATABASE_URL"], ROOT / "migrations"
        )
        replay = fresh_store.get_by_idempotency(
            self.tenant_id,
            self.intent.idempotency_scope,
            self.intent.idempotency_key,
        )
        self.assertIsNotNone(replay)
        self.assertEqual(first.intent.intent_digest(), replay.intent.intent_digest())
        self.assertEqual(DispatchState.PENDING, replay.state)

    def test_pending_work_is_claimable_from_a_fresh_process(self):
        accepted = self.store.accept(self.input, self.intent)
        script = """
import json, os
from pathlib import Path
from work_intake.adapters.postgres import PostgresWorkIntakeStore
store = PostgresWorkIntakeStore(os.environ['WORK_INTAKE_TEST_DATABASE_URL'], Path.cwd() / 'migrations')
claimed = store.claim_pending(limit=1, worker_id='restart-process-dispatcher', lease_seconds=30, tenant_id=os.environ['WORK_INTAKE_TEST_TENANT'])
print(json.dumps({'count': len(claimed), 'submission_id': claimed[0].intent.submission_id if claimed else None, 'state': claimed[0].state.value if claimed else None}))
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env=os.environ | {"WORK_INTAKE_TEST_TENANT": self.tenant_id},
        )

        self.assertEqual(0, result.returncode, result.stderr)
        recovered = json.loads(result.stdout)
        self.assertEqual(1, recovered["count"])
        self.assertEqual(accepted.intent.submission_id, recovered["submission_id"])
        self.assertEqual("claimed", recovered["state"])

    def test_conflicting_logical_input_or_intent_is_rejected(self):
        self.store.accept(self.input, self.intent)
        other_input = LogicalInput(
            self.tenant_id,
            self.input.input_id,
            self.input.source_id,
            "controlled-v2",
            self.input.contract,
        )
        with self.assertRaisesRegex(WorkIntakeError, "logical Input identity"):
            self.store.accept(other_input, self.intent)

    def test_same_scoped_key_conflicts_on_release_binding(self):
        from dataclasses import replace

        self.store.accept(self.input, self.intent)
        changed_release = replace(self.intent, release_id="controlled-release-v2")
        with self.assertRaisesRegex(WorkIntakeError, "conflicting intent"):
            self.store.accept(self.input, changed_release)

    def test_concurrent_identical_submission_acceptance_replays_one_identity(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            accepted = list(
                pool.map(
                    lambda _index: self.store.accept(self.input, self.intent),
                    range(6),
                )
            )
        self.assertEqual(
            {self.intent.submission_id},
            {item.intent.submission_id for item in accepted},
        )
        self.assertEqual(
            {self.intent.intent_digest()},
            {item.intent.intent_digest() for item in accepted},
        )

    def test_same_idempotency_key_is_independent_across_tenants(self):
        from dataclasses import replace

        first = self.store.accept(self.input, self.intent)
        tenant = "other-" + uuid.uuid4().hex
        other_input = LogicalInput(
            tenant,
            self.input.input_id,
            self.input.source_id,
            self.input.source_revision_id,
            self.input.contract,
        )
        other_intent = replace(
            self.intent,
            tenant_id=tenant,
            submission_id=str(uuid.uuid4()),
            dispatch_operation_id="dispatch-" + uuid.uuid4().hex,
        )
        second = self.store.accept(other_input, other_intent)
        self.assertEqual(self.intent.idempotency_key, other_intent.idempotency_key)
        self.assertNotEqual(first.intent.submission_id, second.intent.submission_id)

    def test_concurrent_dispatch_claims_are_exclusive_and_fenced(self):
        self.store.accept(self.input, self.intent)
        second = PostgresWorkIntakeStore(
            os.environ["WORK_INTAKE_TEST_DATABASE_URL"], ROOT / "migrations"
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                self.store.claim_pending,
                limit=1,
                worker_id="dispatcher-one",
                lease_seconds=30,
                tenant_id=self.tenant_id,
            )
            second_future = executor.submit(
                second.claim_pending,
                limit=1,
                worker_id="dispatcher-two",
                lease_seconds=30,
                tenant_id=self.tenant_id,
            )
            claims = first_future.result() + second_future.result()
        self.assertEqual(1, len(claims))
        claim = claims[0]
        admitted = self.store.mark_admitted(
            self.tenant_id,
            self.intent.submission_id,
            claim.claim_owner or "",
            claim.claim_generation,
            str(uuid.uuid4()),
        )
        self.assertEqual(DispatchState.ADMITTED, admitted.state)

    def test_pending_cancellation_persists_and_is_not_reclaimed(self):
        self.store.accept(self.input, self.intent)
        cancelled = self.store.cancel_pending(self.tenant_id, self.intent.submission_id)
        self.assertEqual(DispatchState.CANCELLED, cancelled.state)
        self.assertEqual(
            (),
            self.store.claim_pending(
                limit=10,
                worker_id="dispatcher-cancel-check",
                lease_seconds=30,
                tenant_id=self.tenant_id,
            ),
        )

    def test_existing_admission_receipt_can_be_attached_before_cancellation(self):
        self.store.accept(self.input, self.intent)
        claim = self.store.claim_pending(
            limit=1,
            worker_id="dispatcher-recovered",
            lease_seconds=30,
            tenant_id=self.tenant_id,
        )[0]
        self.store.release_claim(
            self.tenant_id,
            self.intent.submission_id,
            claim.claim_owner or "",
            claim.claim_generation,
        )
        run_id = str(uuid.uuid4())

        recovered = self.store.attach_existing_admission(
            self.tenant_id, self.intent.submission_id, run_id
        )

        self.assertEqual(DispatchState.ADMITTED, recovered.state)
        self.assertEqual(run_id, recovered.run_id)


if __name__ == "__main__":
    unittest.main()
