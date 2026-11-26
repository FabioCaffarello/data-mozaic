from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from mozaic_contract_resolution import (
    ContractReference,
    ContractResolutionError,
)
from work_intake.application import WorkIntake
from work_intake.domain import (
    DispatchRejected,
    DispatchState,
    LogicalInput,
    Submission,
    WorkIntakeError,
)


class _Schema:
    def validate(self, payload):
        if set(payload) != {"page"} or not isinstance(payload["page"], int):
            raise ContractResolutionError("invalid")


class _Resolver:
    def resolve(self, reference):
        return _Schema()


class _Release:
    def __init__(self):
        self.revoked = False

    def resolve_eligible_release(self, release_id):
        if self.revoked:
            raise DispatchRejected("release_revoked")
        return {
            "release_id": release_id,
            "status": "approved",
            "contract_version": "bot-release/v3",
            "source_id": "controlled-http",
            "source_revision_id": "controlled-v1",
            "input_contract": {
                "contract_id": "controlled-input",
                "version": "1.0.0",
                "digest": "sha256:" + "a" * 64,
            },
        }


class _Store:
    def __init__(self):
        self.by_idempotency = {}
        self.claimed = {}

    def accept(self, logical_input, intent):
        key = (intent.tenant_id, intent.idempotency_scope, intent.idempotency_key)
        previous = self.by_idempotency.get(key)
        if previous:
            if previous.intent.intent_digest() != intent.intent_digest():
                raise WorkIntakeError(
                    "idempotency key was reused with conflicting intent"
                )
            return previous
        result = Submission(intent, DispatchState.PENDING)
        self.by_idempotency[key] = result
        return result

    def get_submission(self, tenant_id, submission_id):
        return next(
            (
                item
                for item in self.by_idempotency.values()
                if item.intent.tenant_id == tenant_id
                and item.intent.submission_id == submission_id
            ),
            None,
        )

    def get_by_idempotency(self, tenant_id, idempotency_scope, idempotency_key):
        return self.by_idempotency.get((tenant_id, idempotency_scope, idempotency_key))

    def claim_pending(self, *, limit, worker_id, lease_seconds, tenant_id=None):
        pending = [
            item
            for item in self.by_idempotency.values()
            if item.state == DispatchState.PENDING
            and (tenant_id is None or item.intent.tenant_id == tenant_id)
        ]
        claimed = []
        for item in pending[:limit]:
            generation = item.claim_generation + 1
            updated = replace(
                item,
                state=DispatchState.CLAIMED,
                claim_owner=worker_id,
                claim_generation=generation,
            )
            self.claimed[item.intent.submission_id] = (worker_id, generation)
            self._replace(updated)
            claimed.append(updated)
        return tuple(claimed)

    def mark_admitted(
        self, tenant_id, submission_id, worker_id, claim_generation, run_id
    ):
        if self.claimed.get(submission_id) != (worker_id, claim_generation):
            raise WorkIntakeError("stale claim")
        old = self.get_submission(tenant_id, submission_id)
        result = replace(old, state=DispatchState.ADMITTED, run_id=run_id)
        self._replace(result)
        return result

    def mark_rejected(
        self, tenant_id, submission_id, worker_id, claim_generation, diagnostic_code
    ):
        if self.claimed.get(submission_id) != (worker_id, claim_generation):
            raise WorkIntakeError("stale claim")
        old = self.get_submission(tenant_id, submission_id)
        result = replace(
            old, state=DispatchState.REJECTED, diagnostic_code=diagnostic_code
        )
        self._replace(result)
        return result

    def release_claim(self, tenant_id, submission_id, worker_id, claim_generation):
        self.claimed.pop(submission_id, None)
        old = self.get_submission(tenant_id, submission_id)
        if old is not None and old.state == DispatchState.CLAIMED:
            self._replace(replace(old, state=DispatchState.PENDING, claim_owner=None))

    def cancel_pending(self, tenant_id, submission_id):
        old = self.get_submission(tenant_id, submission_id)
        if old is None:
            raise WorkIntakeError("submission does not exist")
        if old.state == DispatchState.CANCELLED:
            return old
        if old.state != DispatchState.PENDING:
            raise WorkIntakeError("submission is no longer pending")
        result = replace(old, state=DispatchState.CANCELLED)
        self._replace(result)
        return result

    def attach_existing_admission(self, tenant_id, submission_id, run_id):
        old = self.get_submission(tenant_id, submission_id)
        if old.state not in {DispatchState.PENDING, DispatchState.ADMITTED}:
            raise WorkIntakeError("submission changed while resolving its admission")
        result = replace(old, state=DispatchState.ADMITTED, run_id=run_id)
        self._replace(result)
        return result

    def _replace(self, result):
        key = (
            result.intent.tenant_id,
            result.intent.idempotency_scope,
            result.intent.idempotency_key,
        )
        self.by_idempotency[key] = result


class _Admission:
    def __init__(self):
        self.run_by_operation = {}
        self.calls = 0
        self.lose_next_acknowledgment = False

    def resolve_existing(self, submission):
        return self.run_by_operation.get(submission.intent.dispatch_operation_id)

    def admit(self, submission, release):
        self.calls += 1
        operation = submission.intent.dispatch_operation_id
        run_id = self.run_by_operation.setdefault(
            operation, "run-" + str(len(self.run_by_operation) + 1)
        )
        if self.lose_next_acknowledgment:
            self.lose_next_acknowledgment = False
            raise RuntimeError("simulated admission acknowledgment loss")
        return run_id


class _RunQueries:
    def __init__(self):
        self.cancellations = []
        self.inspections = []

    def inspect(self, tenant_id, run_id):
        result = {"tenant_id": tenant_id, "run_id": run_id, "owner": "BC04"}
        self.inspections.append(result)
        return result

    def cancel(self, tenant_id, run_id, operation_id):
        result = {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "operation_id": operation_id,
            "owner": "BC04",
        }
        self.cancellations.append(result)
        return result


class WorkIntakeApplicationTests(unittest.TestCase):
    def setUp(self):
        self.reference = ContractReference(
            "controlled-input", "1.0.0", "sha256:" + "a" * 64
        )
        self.input = LogicalInput(
            "tenant-a",
            "input-quarter",
            "controlled-http",
            "controlled-v1",
            self.reference,
        )
        self.store = _Store()
        self.releases = _Release()
        self.admission = _Admission()
        self.run_queries = _RunQueries()
        self.app = WorkIntake(
            self.store,
            _Resolver(),
            self.releases,
            self.admission,
            outcomes=self.run_queries,
            cancellation=self.run_queries,
        )

    def submit(
        self, key="request-1", page=1, release_id="release-a", logical_input=None
    ):
        return self.app.submit(
            logical_input=logical_input or self.input,
            idempotency_scope="client-a",
            idempotency_key=key,
            release_id=release_id,
            parameters={"page": page},
        )

    def test_same_idempotency_key_and_intent_resolves_original_submission(self):
        first = self.submit()
        replay = self.submit()
        self.assertEqual(first.intent.submission_id, replay.intent.submission_id)
        self.assertEqual(
            first.intent.dispatch_operation_id, replay.intent.dispatch_operation_id
        )
        self.assertEqual(
            first.intent.parameters_digest, replay.intent.parameters_digest
        )

    def test_same_idempotency_key_with_different_parameters_conflicts(self):
        self.submit()
        with self.assertRaisesRegex(WorkIntakeError, "conflicting intent"):
            self.submit(page=2)

    def test_same_idempotency_key_with_different_release_conflicts(self):
        self.submit()
        with self.assertRaisesRegex(WorkIntakeError, "conflicting intent"):
            self.submit(release_id="release-b")

    def test_idempotency_scope_is_tenant_local(self):
        first = self.submit("shared-key")
        other_tenant = LogicalInput(
            "tenant-b",
            "input-quarter",
            "controlled-http",
            "controlled-v1",
            self.reference,
        )
        second = self.submit("shared-key", logical_input=other_tenant)
        self.assertNotEqual(first.intent.submission_id, second.intent.submission_id)
        self.assertEqual("tenant-a", first.intent.tenant_id)
        self.assertEqual("tenant-b", second.intent.tenant_id)

    def test_distinct_submission_can_reacquire_same_logical_input(self):
        first = self.submit("request-1", 1)
        second = self.submit("request-2", 1)
        self.assertEqual(first.intent.input_id, second.intent.input_id)
        self.assertNotEqual(first.intent.submission_id, second.intent.submission_id)

    def test_dispatch_uses_stable_operation_and_records_run(self):
        accepted = self.submit()
        result = self.app.dispatch_pending(worker_id="dispatcher-a")
        self.assertEqual(DispatchState.ADMITTED, result[0].state)
        self.assertEqual(1, self.admission.calls)
        self.assertEqual(accepted.intent.submission_id, result[0].intent.submission_id)

    def test_stale_claim_cannot_confirm_after_a_newer_claim(self):
        self.submit()
        first = self.store.claim_pending(
            limit=1, worker_id="dispatcher-a", lease_seconds=1
        )[0]
        self.store.by_idempotency[
            (
                first.intent.tenant_id,
                first.intent.idempotency_scope,
                first.intent.idempotency_key,
            )
        ] = replace(first, state=DispatchState.PENDING, claim_owner=None)
        second = self.store.claim_pending(
            limit=1, worker_id="dispatcher-b", lease_seconds=1
        )[0]
        self.assertGreater(second.claim_generation, first.claim_generation)
        with self.assertRaisesRegex(WorkIntakeError, "stale claim"):
            self.store.mark_admitted(
                first.intent.tenant_id,
                first.intent.submission_id,
                "dispatcher-a",
                first.claim_generation,
                "00000000-0000-0000-0000-000000000001",
            )

    def test_revoked_release_rejects_before_bc04_admission(self):
        self.submit()
        self.releases.revoked = True
        result = self.app.dispatch_pending(worker_id="dispatcher-a")
        self.assertEqual(DispatchState.REJECTED, result[0].state)
        self.assertEqual("release_revoked", result[0].diagnostic_code)
        self.assertEqual(0, self.admission.calls)

    def test_lost_admission_ack_resolves_original_run_after_release_revocation(self):
        self.submit()
        self.admission.lose_next_acknowledgment = True
        with self.assertRaisesRegex(RuntimeError, "acknowledgment loss"):
            self.app.dispatch_pending(worker_id="dispatcher-a")
        self.assertEqual(1, self.admission.calls)

        self.releases.revoked = True
        recovered = self.app.dispatch_pending(worker_id="dispatcher-b")

        self.assertEqual(DispatchState.ADMITTED, recovered[0].state)
        self.assertEqual("run-1", recovered[0].run_id)
        self.assertEqual(1, self.admission.calls)

    def test_accepted_submission_replay_survives_later_release_revocation(self):
        first = self.submit()
        self.releases.revoked = True
        replay = self.submit()
        self.assertEqual(first.intent.submission_id, replay.intent.submission_id)

    def test_parameter_representation_is_immutable_canonical_json(self):
        submission = self.submit()
        self.assertEqual(b'{"page":1}', submission.intent.parameters_json)
        self.assertEqual({"page": 1}, json.loads(submission.intent.parameters_json))

    def test_persisted_duplicate_or_noncanonical_parameter_json_is_rejected(self):
        submission = self.submit()
        duplicate = b'{"page":1,"page":2}'
        with self.assertRaisesRegex(WorkIntakeError, "parameter bytes are invalid"):
            replace(
                submission.intent,
                parameters_json=duplicate,
                parameters_digest="sha256:" + hashlib.sha256(duplicate).hexdigest(),
            )

    def test_invalid_parameters_do_not_persist(self):
        with self.assertRaises(ContractResolutionError):
            self.app.submit(
                logical_input=self.input,
                idempotency_scope="client-a",
                idempotency_key="invalid",
                release_id="release-a",
                parameters={"page": "1"},
            )
        self.assertFalse(self.store.by_idempotency)

    def test_input_source_binding_must_match_the_approved_release(self):
        wrong_source = replace(self.input, source_revision_id="controlled-v2")
        with self.assertRaisesRegex(WorkIntakeError, "approved release"):
            self.app.submit(
                logical_input=wrong_source,
                idempotency_scope="client-a",
                idempotency_key="wrong-source",
                release_id="release-a",
                parameters={"page": 1},
            )
        self.assertFalse(self.store.by_idempotency)

    def test_pending_work_can_be_cancelled_in_bc13(self):
        accepted = self.submit()
        result = self.app.cancel("tenant-a", accepted.intent.submission_id)
        self.assertEqual(DispatchState.CANCELLED, result.state)
        self.assertEqual(0, self.admission.calls)

    def test_admitted_run_inspection_and_cancellation_remain_bc04_owned(self):
        self.submit()
        admitted = self.app.dispatch_pending(worker_id="dispatcher-a")[0]
        outcome = self.app.inspect_execution("tenant-a", admitted.intent.submission_id)
        self.assertEqual("BC04", outcome["owner"])
        cancelled = self.app.cancel("tenant-a", admitted.intent.submission_id)
        self.assertEqual("BC04", cancelled["owner"])
        self.assertEqual(admitted.run_id, cancelled["run_id"])
        self.assertEqual(1, len(self.run_queries.inspections))
        self.assertEqual(1, len(self.run_queries.cancellations))

    def test_cancellation_reconciles_ambiguous_admission_before_bc04_cancel(self):
        accepted = self.submit()
        claimed = self.store.claim_pending(
            limit=1, worker_id="dispatcher-a", lease_seconds=30
        )[0]
        run_id = self.admission.admit(
            claimed, self.releases.resolve_eligible_release("release-a")
        )
        self.store.release_claim(
            "tenant-a",
            accepted.intent.submission_id,
            "dispatcher-a",
            claimed.claim_generation,
        )

        result = self.app.cancel("tenant-a", accepted.intent.submission_id)

        self.assertEqual("BC04", result["owner"])
        self.assertEqual(run_id, result["run_id"])
        self.assertEqual(
            DispatchState.ADMITTED,
            self.app.inspect("tenant-a", accepted.intent.submission_id).state,
        )
        self.assertEqual(1, len(self.run_queries.cancellations))

    def test_cancellation_stays_unresolved_when_retry_has_no_admission_receipt(self):
        accepted = self.submit()
        claim = self.store.claim_pending(
            limit=1, worker_id="dispatcher-a", lease_seconds=30
        )[0]
        self.store.release_claim(
            "tenant-a",
            accepted.intent.submission_id,
            "dispatcher-a",
            claim.claim_generation,
        )

        with self.assertRaisesRegex(WorkIntakeError, "outcome is unresolved"):
            self.app.cancel("tenant-a", accepted.intent.submission_id)
        self.assertEqual(
            DispatchState.PENDING,
            self.app.inspect("tenant-a", accepted.intent.submission_id).state,
        )


if __name__ == "__main__":
    unittest.main()
