"""Trusted per-execution paths and deadline passed by the local launcher."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExecutionContext:
    output_root: Path
    timeout_seconds: float = 60.0
    termination_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 300:
            raise ValueError(
                "[EXECUTION_TIMEOUT_INVALID] timeout must be within (0, 300]"
            )
        if self.termination_grace_seconds < 0 or self.termination_grace_seconds > 10:
            raise ValueError(
                "[EXECUTION_GRACE_INVALID] termination grace must be within [0, 10]"
            )
