from __future__ import annotations

import threading
import time
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from execution_coordination.application import ExecutionCoordinator
from execution_coordination.domain import (
    CaptureAttempt,
    CaptureRun,
    CoordinationError,
    ExecutionBinding,
    ExecutionReport,
    State,
)
from execution_coordination.repository import InMemoryRunRepository


def binding() -> ExecutionBinding:
    image_digest = "sha256:" + "b" * 64
    return ExecutionBinding(
        source_id="source-1",
        source_revision="revision-1",
        bot_id="bot-" + "c" * 64,
        build_id="d" * 64,
        bot_build_manifest_digest="e" * 64,
        assembly_id="f" * 64,
        catalog_digest="1" * 64,
        oci_image="registry.invalid/scrapy@" + image_digest,
        oci_digest=image_digest,
        local_image_id="sha256:" + "2" * 64,
        fixture_bundle_id="fixture-bundle-1",
        fixture_bundle_digest="3" * 64,
    )


def report(run: CaptureRun, status: str = "succeeded") -> ExecutionReport:
    assert run.attempt
    return ExecutionReport(
        "execution-report/v0",
        run.request_id,
        run.run_id,
        run.attempt.attempt_id,
        run.attempt.execution_id,
        status,
        run.binding,
        5,
        records_count=1 if status == "succeeded" else 0,
        output_bytes=10 if status == "succeeded" else 0,
        output_sha256="4" * 64 if status == "succeeded" else None,
        output_ref="stage" if status == "succeeded" else None,
    )


class DomainTests(unittest.TestCase):
    def run_model(self) -> CaptureRun:
        return CaptureRun(
            str(uuid.uuid4()),
            "request-1",
            "tenant:demo",
            "key-1",
            binding(),
            datetime.now(UTC) + timedelta(seconds=30),
            attempt=CaptureAttempt(str(uuid.uuid4()), execution_id=str(uuid.uuid4())),
        )

    def test_terminal_success_is_idempotent_and_conflict_is_rejected(self) -> None:
        run = self.run_model()
        run.set_state(State.LAUNCHING)
        self.assertEqual(run.attempt.state, State.LAUNCHING)
        run.set_state(State.RUNNING)
        self.assertEqual(run.attempt.state, State.RUNNING)
        value = report(run)
        self.assertTrue(run.reconcile(value))
        self.assertEqual(run.state, State.SUCCEEDED)
        self.assertFalse(run.reconcile(value))
        with self.assertRaises(CoordinationError):
            run.reconcile(report(run, "failed"))

    def test_cancel_wins_against_late_success(self) -> None:
        run = self.run_model()
        run.set_state(State.LAUNCHING)
        run.set_state(State.RUNNING)
        run.set_state(State.CANCEL_REQUESTED)
        run.reconcile(report(run))
        self.assertEqual(run.state, State.CANCELLED)
        self.assertEqual(run.attempt.state, State.CANCELLED)
        with self.assertRaises(CoordinationError):
            run.reconcile(report(run, "failed"))

    def test_prohibited_state_transitions(self) -> None:
        run = self.run_model()
        with self.assertRaises(CoordinationError):
            run.set_state(State.SUCCEEDED)
        run.set_state(State.LAUNCHING)
        run.set_state(State.RUNNING)
        run.set_state(State.TIMED_OUT)
        with self.assertRaises(CoordinationError):
            run.set_state(State.RUNNING)

    def test_cancel_before_launch_has_terminal_run_and_attempt(self) -> None:
        run = self.run_model()
        run.set_state(State.CANCEL_REQUESTED)
        run.set_state(State.CANCELLED)
        self.assertEqual(run.state, State.CANCELLED)
        self.assertEqual(run.attempt.state, State.CANCELLED)

    def test_success_cannot_be_set_without_a_verified_report(self) -> None:
        run = self.run_model()
        run.set_state(State.LAUNCHING)
        run.set_state(State.RUNNING)
        with self.assertRaises(CoordinationError):
            run.set_state(State.SUCCEEDED)
        with self.assertRaises(CoordinationError):
            run.attempt.set_state(State.SUCCEEDED)

    def test_deadline_wins_over_a_success_report_arriving_late(self) -> None:
        run = self.run_model()
        run.deadline = datetime.now(UTC) - timedelta(seconds=1)
        run.set_state(State.LAUNCHING)
        run.set_state(State.RUNNING)
        self.assertTrue(run.reconcile(report(run)))
        self.assertEqual(run.state, State.TIMED_OUT)
        self.assertEqual(run.attempt.state, State.TIMED_OUT)
        self.assertIsNone(run.terminal_report)

    def test_attempt_rejects_prohibited_transition(self) -> None:
        attempt = CaptureAttempt(str(uuid.uuid4()))
        with self.assertRaises(CoordinationError):
            attempt.set_state(State.SUCCEEDED)
        attempt.set_state(State.LAUNCHING)
        attempt.set_state(State.RUNNING)
        attempt.set_state(State.CRASHED)
        with self.assertRaises(CoordinationError):
            attempt.set_state(State.RUNNING)

    def test_wrong_attempt_report_is_rejected(self) -> None:
        run = self.run_model()
        other = report(run)
        run.attempt.attempt_id = str(uuid.uuid4())
        with self.assertRaises(CoordinationError):
            run.reconcile(other)


class IdempotencyTests(unittest.TestCase):
    def test_same_scoped_key_and_same_intent_returns_existing_run(self) -> None:
        repo = InMemoryRunRepository()
        deadline = datetime.now(UTC) + timedelta(seconds=20)
        first = CaptureRun(
            str(uuid.uuid4()), "req-a", "tenant-a", "key", binding(), deadline
        )
        second = CaptureRun(
            str(uuid.uuid4()), "req-b", "tenant-a", "key", binding(), deadline
        )
        stored, created = repo.start_run(first)
        replay, replay_created = repo.start_run(second)
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay.run_id, stored.run_id)

    def test_same_key_with_different_intent_is_conflict(self) -> None:
        repo = InMemoryRunRepository()
        deadline = datetime.now(UTC) + timedelta(seconds=20)
        first = CaptureRun(
            str(uuid.uuid4()), "req-a", "tenant-a", "key", binding(), deadline
        )
        changed = replace(binding(), source_revision="revision-2")
        second = CaptureRun(
            str(uuid.uuid4()), "req-b", "tenant-a", "key", changed, deadline
        )
        repo.start_run(first)
        with self.assertRaises(CoordinationError):
            repo.start_run(second)


class _Handle:
    def __init__(
        self,
        request,
        *,
        delay: float = 0.05,
        status: str = "succeeded",
        ignore_cancel: bool = False,
        wrong_identity: bool = False,
    ):
        self.request = request
        self.execution_id = request.execution_id
        self.delay = delay
        self.status = status
        self.ignore_cancel = ignore_cancel
        self.wrong_identity = wrong_identity
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def wait(self, deadline):
        self.started.set()
        if self.ignore_cancel:
            time.sleep(self.delay)
            status = self.status
        elif self.cancelled.wait(self.delay):
            status = "cancelled"
        else:
            status = self.status
        return ExecutionReport(
            "execution-report/v0",
            self.request.request_id,
            str(uuid.uuid4()) if self.wrong_identity else self.request.run_id,
            self.request.attempt_id,
            self.request.execution_id,
            status,
            self.request.binding,
            2,
            1 if status == "succeeded" else 0,
            10 if status == "succeeded" else 0,
            "4" * 64 if status == "succeeded" else None,
            "staging" if status == "succeeded" else None,
        )

    def cancel(self):
        self.cancelled.set()


class _Executor:
    def __init__(
        self,
        *,
        ignore_cancel: bool = False,
        status: str = "succeeded",
        wrong_identity: bool = False,
        start_error: Exception | None = None,
        start_delay: float = 0,
    ):
        self.ignore_cancel = ignore_cancel
        self.status = status
        self.wrong_identity = wrong_identity
        self.start_error = start_error
        self.start_delay = start_delay
        self.handles = []
        self.discarded = []
        self.published = []

    def start(self, request):
        if self.start_delay:
            time.sleep(self.start_delay)
        if self.start_error:
            raise self.start_error
        handle = _Handle(
            request,
            status=self.status,
            ignore_cancel=self.ignore_cancel,
            wrong_identity=self.wrong_identity,
        )
        self.handles.append(handle)
        return handle

    def publish(self, value):
        self.published.append(value)
        from dataclasses import replace

        return replace(value, output_ref="published")

    def discard(self, value):
        self.discarded.append(value)


class CoordinatorTests(unittest.TestCase):
    def _submit(self, coordinator, key: str, deadline_seconds: float = 5):
        return coordinator.submit(
            idempotency_scope="tenant:demo",
            idempotency_key=key,
            request_id=f"req-{key}",
            binding=binding(),
            deadline=datetime.now(UTC) + timedelta(seconds=deadline_seconds),
        )

    def test_success_is_published_only_after_terminal_acceptance(self):
        executor = _Executor()
        coordinator = ExecutionCoordinator(InMemoryRunRepository(), executor)
        try:
            run = coordinator.submit(
                idempotency_scope="tenant:demo",
                idempotency_key="capture-1",
                request_id="req-1",
                binding=binding(),
                deadline=datetime.now(UTC) + timedelta(seconds=5),
            )
            finished = coordinator.wait(run.run_id, timeout=2)
            self.assertEqual(finished.state, State.SUCCEEDED)
            self.assertEqual(len(executor.published), 1)
        finally:
            coordinator.shutdown()

    def test_start_failure_is_terminal_before_running(self):
        executor = _Executor(start_error=RuntimeError("container rejected"))
        coordinator = ExecutionCoordinator(InMemoryRunRepository(), executor)
        try:
            run = self._submit(coordinator, "start-failure")
            finished = coordinator.wait(run.run_id, timeout=2)
            self.assertEqual(finished.state, State.FAILED)
            self.assertEqual(finished.attempt.state, State.FAILED)
            self.assertIsNone(finished.attempt.report)
        finally:
            coordinator.shutdown()

    def test_deadline_expiring_during_startup_wins_over_success(self):
        executor = _Executor(start_delay=0.05)
        coordinator = ExecutionCoordinator(InMemoryRunRepository(), executor)
        try:
            run = self._submit(coordinator, "startup-deadline", deadline_seconds=0.02)
            finished = coordinator.wait(run.run_id, timeout=2)
            self.assertEqual(finished.state, State.TIMED_OUT)
            self.assertEqual(finished.attempt.state, State.TIMED_OUT)
            self.assertEqual(executor.published, [])
            self.assertEqual(len(executor.discarded), 1)
        finally:
            coordinator.shutdown()

    def test_live_cancel_has_control_path_and_discards_late_success(self):
        executor = _Executor(ignore_cancel=True)
        coordinator = ExecutionCoordinator(InMemoryRunRepository(), executor)
        try:
            run = coordinator.submit(
                idempotency_scope="tenant:demo",
                idempotency_key="capture-2",
                request_id="req-2",
                binding=binding(),
                deadline=datetime.now(UTC) + timedelta(seconds=5),
            )
            deadline = time.monotonic() + 2
            while not executor.handles and time.monotonic() < deadline:
                time.sleep(0.005)
            handle = executor.handles[0]
            self.assertTrue(handle.started.wait(1))
            coordinator.cancel(run.run_id)
            finished = coordinator.wait(run.run_id, timeout=2)
            self.assertEqual(finished.state, State.CANCELLED)
            self.assertFalse(executor.published)
            self.assertEqual(len(executor.discarded), 1)
        finally:
            coordinator.shutdown()

    def test_mismatched_report_is_rejected_before_output_publication(self):
        executor = _Executor(wrong_identity=True)
        coordinator = ExecutionCoordinator(InMemoryRunRepository(), executor)
        try:
            run = coordinator.submit(
                idempotency_scope="tenant:demo",
                idempotency_key="mismatched-report",
                request_id="req-mismatch",
                binding=binding(),
                deadline=datetime.now(UTC) + timedelta(seconds=5),
            )
            finished = coordinator.wait(run.run_id, timeout=2)
            self.assertEqual(finished.state, State.FAILED)
            self.assertEqual(executor.published, [])
            self.assertEqual(len(executor.discarded), 1)
        finally:
            coordinator.shutdown()


if __name__ == "__main__":
    unittest.main()
