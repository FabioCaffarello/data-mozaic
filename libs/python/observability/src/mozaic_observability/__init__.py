"""Safe structured event helpers; logger configuration remains application-owned."""

from .events import bind_context, log_event

__all__ = ["bind_context", "log_event"]
