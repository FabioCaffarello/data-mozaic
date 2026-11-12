"""Structured stdlib logging without global configuration or free-form payloads."""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager

_ALLOWED_FIELDS = frozenset(
    {
        "session_id",
        "command_id",
        "effect_id",
        "event_id",
        "worker_id",
        "attempt",
        "component",
        "outcome",
        "state",
        "reason",
    }
)
_context: contextvars.ContextVar[dict[str, str | int] | None] = contextvars.ContextVar(
    "mozaic_observability_context", default=None
)


def _safe_fields(fields: dict[str, object]) -> dict[str, str | int]:
    safe: dict[str, str | int] = {}
    for key, value in fields.items():
        if key not in _ALLOWED_FIELDS:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            safe[key] = value
        elif isinstance(value, str):
            # IDs/state labels are bounded. Raw content and arbitrary objects are excluded.
            safe[key] = value[:160]
    return safe


@contextmanager
def bind_context(**fields: object) -> Iterator[None]:
    """Temporarily bind allowlisted correlation fields in this execution context."""
    token = _context.set((_context.get() or {}) | _safe_fields(fields))
    try:
        yield
    finally:
        _context.reset(token)


def log_event(
    logger: logging.Logger,
    level: int,
    event_name: str,
    **fields: object,
) -> None:
    """Emit a static event name plus safe structured fields on an application logger."""
    safe_name = event_name[:120] if event_name.isascii() else "mozaic.event"
    logger.log(
        level,
        safe_name,
        extra={
            "mozaic_event": safe_name,
            "mozaic_context": (_context.get() or {}).copy(),
            "mozaic_fields": _safe_fields(fields),
        },
    )
