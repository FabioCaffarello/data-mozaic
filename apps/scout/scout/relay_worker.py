"""Independent lifecycle for the Scout BC15 outbox relay."""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
from threading import Event

from .adapters.postgres.repository import PostgresScoutSessionRepository
from .outbox_relay import OutboxRelay

_LOGGER = logging.getLogger(__name__)


def run_worker(
    relay: OutboxRelay,
    stop: Event,
    *,
    idle_poll_seconds: float = 1.0,
    max_messages: int | None = None,
) -> int:
    """Run until signaled; finish the in-flight item before graceful shutdown."""
    if idle_poll_seconds <= 0:
        raise ValueError("idle_poll_seconds must be positive")
    processed = 0
    while not stop.is_set():
        result = relay.run_once()
        if result == "idle":
            if max_messages is not None:
                break
            stop.wait(idle_poll_seconds)
            continue
        processed += 1
        if max_messages is not None and processed >= max_messages:
            break
    return processed


def _load_factory(spec: str):
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("receiver factory must use module:callable syntax")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise TypeError("receiver factory is not callable")
    return factory


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(
        description="Run the independent Scout outbox relay"
    )
    parser.add_argument(
        "--once", action="store_true", help="process at most one item and exit"
    )
    parser.add_argument("--idle-poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)

    dsn = os.environ["SCOUT_DATABASE_URL"]
    factory_spec = os.environ["SCOUT_BC15_RECEIVER_FACTORY"]
    receiver = _load_factory(factory_spec)(dsn)
    repository = PostgresScoutSessionRepository(dsn)
    relay = OutboxRelay(repository, receiver)
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    processed = run_worker(
        relay,
        stop,
        idle_poll_seconds=args.idle_poll_seconds,
        max_messages=1 if args.once else None,
    )
    _LOGGER.info("scout.relay.stopped", extra={"processed_count": processed})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
