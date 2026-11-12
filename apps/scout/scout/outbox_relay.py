"""Scout-specific at-least-once relay for durable BC15 proposal intents."""

from __future__ import annotations

import logging
import os
import random
import socket
from datetime import UTC, datetime
from uuid import uuid4

from mozaic_observability import bind_context, log_event

_LOGGER = logging.getLogger(__name__)


class OutboxRelay:
    def __init__(
        self,
        repository,
        receiver,
        *,
        owner: str | None = None,
        lease_seconds: int = 30,
        max_attempts: int = 5,
        base_backoff_seconds: int = 2,
        max_backoff_seconds: int = 300,
        jitter=None,
    ):
        self.repository = repository
        self.receiver = receiver
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.jitter = jitter or (lambda: random.randint(0, self.base_backoff_seconds))

    def run_once(self, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        claim = self.repository.claim_handoff(self.owner, self.lease_seconds, now)
        if claim is None:
            return "idle"
        with bind_context(
            event_id=claim["event_id"],
            worker_id=self.owner,
            attempt=claim["attempt_count"],
            component="scout-outbox-relay",
        ):
            log_event(_LOGGER, logging.INFO, "scout.relay.claimed", state="leased")
        try:
            # The stable outbox event ID is the receiver's deduplication key.
            receipt = self.receiver.deliver_message(claim["event_id"], claim["payload"])
        except Exception as exc:  # noqa: BLE001 - remote outcome must remain recoverable
            attempts = claim["attempt_count"]
            error_class = (
                "transient"
                if isinstance(exc, OSError)
                else "definitive"
                if isinstance(exc, (ValueError, TypeError))
                else "uncertain"
            )
            definitive = error_class == "definitive"
            terminal = definitive or attempts >= self.max_attempts
            exponent = min(attempts - 1, 30)
            delay = min(
                self.base_backoff_seconds * (2**exponent), self.max_backoff_seconds
            )
            retry_delay = min(delay + max(0, self.jitter()), self.max_backoff_seconds)
            accepted = self.repository.finish_handoff(
                claim["event_id"],
                self.owner,
                claim["fencing_token"],
                status="dead_letter" if terminal else "retry_scheduled",
                error_class=error_class,
                result=type(exc).__name__,
                next_attempt_delay_seconds=retry_delay,
            )
            if not accepted:
                return "stale_claim"
            with bind_context(event_id=claim["event_id"], worker_id=self.owner):
                log_event(
                    _LOGGER,
                    logging.WARNING,
                    "scout.relay.failed",
                    attempt=attempts,
                    outcome=error_class,
                    reason=type(exc).__name__,
                )
            return "dead_letter" if terminal else "retry_scheduled"
        if (
            not isinstance(receipt, dict)
            or receipt.get("message_id") != claim["event_id"]
            or receipt.get("status") != "received"
        ):
            accepted = self.repository.finish_handoff(
                claim["event_id"],
                self.owner,
                claim["fencing_token"],
                status="dead_letter",
                error_class="definitive",
                result="invalid_receiver_ack",
                next_attempt_delay_seconds=0,
            )
            return "dead_letter" if accepted else "stale_claim"
        accepted = self.repository.finish_handoff(
            claim["event_id"],
            self.owner,
            claim["fencing_token"],
            status="sent",
            receipt=receipt,
            error_class=None,
            result="received_by_bc15",
            next_attempt_delay_seconds=0,
        )
        if not accepted:
            return "stale_claim"
        with bind_context(event_id=claim["event_id"], worker_id=self.owner):
            log_event(
                _LOGGER,
                logging.INFO,
                "scout.relay.received_by_bc15",
                attempt=claim["attempt_count"],
                outcome="received",
            )
        return "delivered"
