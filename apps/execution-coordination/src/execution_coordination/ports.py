"""BC04-owned ports; deliberately free of engine and infrastructure imports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from execution_coordination.domain import CaptureRun, ExecutionReport, ExecutionRequest


class ExecutionPortError(RuntimeError):
    """A technical adapter could not start, supervise, or publish an execution."""


class RunRepositoryPort(Protocol):
    def start_run(self, run: CaptureRun) -> tuple[CaptureRun, bool]: ...
    def get_run(self, run_id: str) -> CaptureRun | None: ...
    def update(
        self, run_id: str, mutate: Callable[[CaptureRun], None]
    ) -> CaptureRun: ...


class ExecutionHandle(Protocol):
    execution_id: str

    def wait(self, deadline: datetime) -> ExecutionReport: ...
    def cancel(self) -> None: ...


class ExecutionPort(Protocol):
    def start(self, request: ExecutionRequest) -> ExecutionHandle: ...
    def publish(self, report: ExecutionReport) -> ExecutionReport: ...
    def discard(self, report: ExecutionReport) -> None: ...
