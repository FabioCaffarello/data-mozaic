"""Inspect and recover one explicitly scoped BC04 run."""

from __future__ import annotations

import argparse
import os
import sys

from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from execution_coordination.durable_application import DurableExecutionCoordinator


class _PublicationNotConfigured:
    def publish_grant(self, _grant):
        raise RuntimeError("scoped lease recovery cannot publish outputs")

    def get_publication_receipt(self, _tenant_id, _output_set_id):
        raise RuntimeError("scoped lease recovery cannot inspect publication")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    dsn = os.environ.get("BC04_DATABASE_URL")
    if not dsn:
        raise SystemExit("BC04_DATABASE_URL is required")
    coordinator = DurableExecutionCoordinator(
        PostgresDurableRunRepository(dsn),
        _PublicationNotConfigured(),
        grant_signer=None,
    )
    snapshot = coordinator.recover_expired_run(args.tenant_id, args.run_id)
    print(
        f"tenant={snapshot.intent.tenant_id} run={snapshot.intent.run_id} "
        f"state={snapshot.state.value} generation={snapshot.generation} "
        f"attempt_state={snapshot.attempt_state.value if snapshot.attempt_state else '-'} "
        f"lease_until={snapshot.lease_until.isoformat() if snapshot.lease_until else '-'} "
        f"reservation={'present' if snapshot.reservation else 'absent'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
