"""BC04 application service coordinating Runs without engine dependencies."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from execution_coordination.domain import (
    CaptureAttempt,
    CaptureRun,
    CoordinationError,
    ExecutionBinding,
    ExecutionReport,
    ExecutionRequest,
    State,
)
from execution_coordination.ports import (
    ExecutionPort,
    ExecutionPortError,
    RunRepositoryPort,
)


class ExecutionCoordinator:
    """Local fixture coordinator; no durable recovery or automatic retry guarantee."""

    def __init__(
        self,
        repository: RunRepositoryPort,
        executor: ExecutionPort,
        *,
        max_active: int = 2,
    ) -> None:
        self._repository = repository
        self._executor = executor
        self._pool = ThreadPoolExecutor(
            max_workers=max_active, thread_name_prefix="bc04-attempt"
        )
        self._lock = threading.RLock()
        self._handles: dict[str, Any] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._futures: dict[str, Future[None]] = {}

    def submit(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_id: str,
        binding: ExecutionBinding,
        deadline: datetime,
    ) -> CaptureRun:
        if deadline.tzinfo is None:
            raise CoordinationError("deadline must be timezone-aware")
        if deadline <= datetime.now(UTC):
            raise CoordinationError("deadline must be in the future")
        attempt = CaptureAttempt(attempt_id=str(uuid.uuid4()))
        run = CaptureRun(
            run_id=str(uuid.uuid4()),
            request_id=request_id,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            binding=binding,
            deadline=deadline,
            attempt=attempt,
        )
        run, created = self._repository.start_run(run)
        if not created:
            return run
        cancel_event = threading.Event()
        with self._lock:
            self._cancel[run.run_id] = cancel_event
            self._futures[run.run_id] = self._pool.submit(
                self._coordinate, run.run_id, cancel_event
            )
        return run

    def get(self, run_id: str) -> CaptureRun:
        run = self._repository.get_run(run_id)
        if run is None:
            raise CoordinationError("run does not exist")
        return run

    def wait(self, run_id: str, timeout: float | None = None) -> CaptureRun:
        with self._lock:
            future = self._futures.get(run_id)
        if future is None:
            return self.get(run_id)
        future.result(timeout=timeout)
        return self.get(run_id)

    def cancel(self, run_id: str) -> CaptureRun:
        def request_cancel(run: CaptureRun) -> None:
            if run.state in {
                State.CANCELLED,
                State.TIMED_OUT,
                State.FAILED,
                State.CRASHED,
                State.SUCCEEDED,
            }:
                return
            if _remaining(run.deadline) <= 0:
                run.set_state(State.TIMED_OUT)
                return
            run.set_state(State.CANCEL_REQUESTED)

        with self._lock:
            run = self._repository.update(run_id, request_cancel)
            event = self._cancel.get(run_id)
            handle = self._handles.get(run_id)
            if event and run.state in {State.CANCEL_REQUESTED, State.TIMED_OUT}:
                event.set()
        if handle is not None:
            handle.cancel()
        return self.get(run_id)

    def shutdown(self, *, wait: bool = True) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=False)

    def _coordinate(self, run_id: str, cancel_event: threading.Event) -> None:
        run = self.get(run_id)
        attempt = run.attempt
        assert attempt is not None
        request = ExecutionRequest(
            contract_version="execution-request/v0",
            request_id=run.request_id,
            run_id=run.run_id,
            attempt_id=attempt.attempt_id,
            binding=run.binding,
            deadline=run.deadline,
            correlation={},
        )
        self._repository.update(
            run_id,
            lambda item: setattr(item.attempt, "execution_id", request.execution_id),
        )
        current = self.get(run_id)
        if current.state == State.CANCEL_REQUESTED:
            self._terminal_without_report(
                run_id, State.CANCELLED, "cancelled before launch"
            )
            return
        launched = False

        def mark_launching(item: CaptureRun) -> None:
            nonlocal launched
            if item.state == State.ACCEPTED:
                item.set_state(State.LAUNCHING)
                launched = True

        self._repository.update(run_id, mark_launching)
        if not launched:
            self._terminal_without_report(
                run_id, State.CANCELLED, "cancelled before launch"
            )
            return
        if _remaining(run.deadline) <= 0:
            self._terminal_without_report(
                run_id, State.TIMED_OUT, "deadline before launch"
            )
            return
        try:
            handle = self._executor.start(request)
        except Exception as error:  # noqa: BLE001 - port boundary fails closed
            self._terminal_without_report(
                run_id,
                State.FAILED,
                f"launch failed: {type(error).__name__}: {str(error)[:500]}",
            )
            return
        with self._lock:
            self._handles[run_id] = handle
            cancel_during_launch = cancel_event.is_set()
        if cancel_during_launch:
            handle.cancel()

        def mark_running(item: CaptureRun) -> None:
            if item.state == State.LAUNCHING:
                item.set_state(State.RUNNING)
                if item.attempt:
                    item.attempt.execution_id = handle.execution_id
            elif item.attempt:
                item.attempt.execution_id = handle.execution_id

        self._repository.update(run_id, mark_running)
        try:
            report = handle.wait(run.deadline)
        except TimeoutError:
            handle.cancel()
            report = _synthetic_report(
                request, handle.execution_id, "timed_out", "container deadline exceeded"
            )
        except (ExecutionPortError, OSError, ValueError) as error:
            report = _synthetic_report(
                request,
                handle.execution_id,
                "crashed",
                f"executor error: {type(error).__name__}",
            )
        except Exception as error:  # noqa: BLE001 - port boundary fails closed
            report = _synthetic_report(
                request,
                handle.execution_id,
                "crashed",
                f"unexpected executor error: {type(error).__name__}",
            )
        with self._lock:
            self._handles.pop(run_id, None)
            current = self.get(run_id)
            if (
                current.state
                not in {
                    State.SUCCEEDED,
                    State.FAILED,
                    State.TIMED_OUT,
                    State.CANCELLED,
                    State.CRASHED,
                    State.CANCEL_REQUESTED,
                }
                and _remaining(current.deadline) <= 0
            ):
                self._terminal_without_report(
                    run_id,
                    State.TIMED_OUT,
                    "Run deadline elapsed before report acceptance",
                )
                self._executor.discard(report)
            elif current.state == State.CANCEL_REQUESTED:
                self._repository.update(run_id, lambda item: item.reconcile(report))
                self._executor.discard(report)
            elif current.state in {
                State.SUCCEEDED,
                State.FAILED,
                State.TIMED_OUT,
                State.CANCELLED,
                State.CRASHED,
            }:
                self._executor.discard(report)
            elif report.technical_status == "succeeded":
                try:
                    self._repository.update(
                        run_id, lambda item: item.validate_report(report)
                    )
                    published = self._executor.publish(report)
                    self._repository.update(
                        run_id, lambda item: item.reconcile(published)
                    )
                except Exception as error:  # noqa: BLE001 - application boundary
                    self._executor.discard(report)
                    self._terminal_without_report(
                        run_id,
                        State.FAILED,
                        f"verified output publication failed: {type(error).__name__}",
                    )
            else:
                try:
                    self._repository.update(run_id, lambda item: item.reconcile(report))
                except CoordinationError:
                    self._executor.discard(report)
                    self._terminal_without_report(
                        run_id,
                        State.FAILED,
                        "execution report identity or terminal state conflict",
                    )

    def _terminal_without_report(
        self, run_id: str, state: State, diagnostic: str
    ) -> None:
        def finish(run: CaptureRun) -> None:
            if run.state == State.CANCEL_REQUESTED:
                state_to_use = State.CANCELLED
            elif _remaining(run.deadline) <= 0:
                state_to_use = State.TIMED_OUT
            else:
                state_to_use = state
            if run.state not in {
                State.SUCCEEDED,
                State.FAILED,
                State.TIMED_OUT,
                State.CANCELLED,
                State.CRASHED,
            }:
                run.set_state(state_to_use)
                if run.attempt:
                    run.attempt.diagnostic = diagnostic

        self._repository.update(run_id, finish)


def _remaining(deadline: datetime) -> float:
    return max(0.0, (deadline - datetime.now(UTC)).total_seconds())


def _synthetic_report(
    request: ExecutionRequest, execution_id: str, status: str, diagnostic: str
) -> ExecutionReport:
    return ExecutionReport(
        contract_version="execution-report/v0",
        request_id=request.request_id,
        run_id=request.run_id,
        attempt_id=request.attempt_id,
        execution_id=execution_id,
        technical_status=status,
        binding=request.binding,
        duration_ms=0,
        diagnostics=({"code": status.upper(), "message": diagnostic},),
    )
