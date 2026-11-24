"""In-memory BC04 repository; atomic only within this coordinator process."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable

from execution_coordination.domain import CaptureRun, CoordinationError


class InMemoryRunRepository:
    """Test/fixture adapter. State is intentionally lost when its process exits."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, CaptureRun] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    def start_run(self, run: CaptureRun) -> tuple[CaptureRun, bool]:
        key = (run.idempotency_scope, run.idempotency_key)
        with self._lock:
            existing_id = self._idempotency.get(key)
            if existing_id:
                existing = self._runs[existing_id]
                if existing.binding != run.binding or existing.deadline != run.deadline:
                    raise CoordinationError(
                        "idempotency key reused with different intent"
                    )
                return copy.deepcopy(existing), False
            if run.run_id in self._runs:
                raise CoordinationError("run identity already exists")
            self._runs[run.run_id] = copy.deepcopy(run)
            self._idempotency[key] = run.run_id
            return copy.deepcopy(run), True

    def get_run(self, run_id: str) -> CaptureRun | None:
        with self._lock:
            run = self._runs.get(run_id)
            return copy.deepcopy(run) if run else None

    def update(self, run_id: str, mutate: Callable[[CaptureRun], None]) -> CaptureRun:
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise CoordinationError("run does not exist")
            candidate = copy.deepcopy(current)
            mutate(candidate)
            self._runs[run_id] = candidate
            return copy.deepcopy(candidate)
