from __future__ import annotations

import unittest
from types import SimpleNamespace

from acquisition_application.operational import OperationalAcquisition


class _WorkIntake:
    def __init__(self, submission, execution):
        self.submission = submission
        self.execution = execution

    def inspect(self, _tenant_id, _submission_id):
        return self.submission

    def inspect_execution(self, _tenant_id, _submission_id):
        return self.execution


class _Coordinator:
    def __init__(self, before, after=None):
        self.before = before
        self.after = after or before
        self.calls = []

    def inspect(self, _tenant_id, _run_id):
        self.calls.append("inspect")
        return self.before if self.calls.count("inspect") == 1 else self.after

    def acquire_attempt(self, *args):
        self.calls.append(("acquire", args))
        return "current-lease"

    def issue_execution_capability(self, lease, *, audience):
        self.calls.append(("capability", lease, audience))
        return {"capability": "BC04-issued"}

    def reconcile_finalization(self, tenant_id, run_id):
        self.calls.append(("reconcile", tenant_id, run_id))


class _ProtectedAttempt:
    def __init__(self):
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return "verified-worker-result-location"


class _TrustedImport:
    def __init__(self):
        self.calls = []

    def import_attempt(self, **kwargs):
        self.calls.append(kwargs)
        return "published-output-set"


class OperationalAcquisitionTests(unittest.TestCase):
    def _workflow(self, *, submission_state="admitted", execution_state="running"):
        submission = SimpleNamespace(
            intent=SimpleNamespace(tenant_id="tenant-a", submission_id="submission-a"),
            run_id="run-a",
            state=SimpleNamespace(value=submission_state),
        )
        execution = SimpleNamespace(
            intent=SimpleNamespace(run_id="run-a"),
            state=SimpleNamespace(value=execution_state),
        )
        run = SimpleNamespace(reservation=None, state=SimpleNamespace(value="running"))
        succeeded = SimpleNamespace(
            reservation=object(),
            state=SimpleNamespace(value="succeeded"),
            publication_receipt={"output_set_id": "set-a"},
        )
        work_intake = _WorkIntake(submission, execution)
        coordinator = _Coordinator(run, succeeded)
        protected = _ProtectedAttempt()
        importer = _TrustedImport()
        return (
            OperationalAcquisition(
                work_intake=work_intake,
                coordinator=coordinator,
                protected_attempt=protected,
                trusted_import=importer,
            ),
            coordinator,
            protected,
            importer,
        )

    def test_executes_only_admitted_run_with_bc04_issued_capability_and_trusted_import(
        self,
    ):
        workflow, coordinator, protected, importer = self._workflow()
        result = workflow.execute(
            tenant_id="tenant-a", submission_id="submission-a", worker_id="operator"
        )
        self.assertEqual(result["state"], "succeeded")
        self.assertFalse(result["replayed"])
        self.assertEqual(
            protected.calls[0]["capability"], {"capability": "BC04-issued"}
        )
        self.assertEqual(protected.calls[0]["lease"], "current-lease")
        self.assertEqual(importer.calls[0]["result"], "verified-worker-result-location")
        self.assertEqual(
            coordinator.calls,
            [
                "inspect",
                (
                    "acquire",
                    ("tenant-a", "run-a", "operator", "operator-attempt-submission-a"),
                ),
                ("capability", "current-lease", "data-mozaic/acquisition-gateway"),
                "inspect",
            ],
        )

    def test_does_not_execute_unadmitted_submission(self):
        workflow, _coordinator, protected, importer = self._workflow(
            submission_state="pending"
        )
        with self.assertRaisesRegex(ValueError, "BC04-admitted"):
            workflow.execute(
                tenant_id="tenant-a", submission_id="submission-a", worker_id="operator"
            )
        self.assertEqual(protected.calls, [])
        self.assertEqual(importer.calls, [])

    def test_completed_submission_replay_does_not_claim_or_execute_again(self):
        workflow, coordinator, protected, importer = self._workflow(
            execution_state="succeeded"
        )
        result = workflow.execute(
            tenant_id="tenant-a", submission_id="submission-a", worker_id="operator"
        )
        self.assertTrue(result["replayed"])
        self.assertEqual(protected.calls, [])
        self.assertEqual(importer.calls, [])
        self.assertEqual(coordinator.calls, [])


if __name__ == "__main__":
    unittest.main()
