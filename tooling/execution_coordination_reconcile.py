"""Run one bounded BC04 finalization reconciliation pass."""

from __future__ import annotations

import argparse
import json
import os
import sys

from acquisition_output.adapters.mongodb import MongoOutputStore, MongoResources
from acquisition_output.adapters.mongodb.config import MongoSettings
from acquisition_output.adapters.publication_grant import (
    Ed25519PublicationGrantVerifier,
)
from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import OutputError
from acquisition_output.wire import publication_receipt_to_wire
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from execution_coordination.durable_application import (
    DurableExecutionCoordinator,
    FinalizationRecoveryState,
)


class _StagingNotConfigured:
    def validate(self, *_args, **_kwargs) -> None:
        raise OutputError("reconciliation composition cannot stage payloads")

    def confirm(self, *_args, **_kwargs):
        raise OutputError("reconciliation composition cannot upload artifacts")


class _BC05PublicationAdapter:
    """Compose only BC05's published application API and public wire receipts."""

    def __init__(self, app: AcquisitionOutputApplication) -> None:
        self._app = app

    def publish_grant(self, grant):
        from acquisition_output.domain import ScopedOperationIdentity

        receipt = self._app.publish_output_set(
            grant.tenant_id,
            grant.output_set_id,
            grant.manifest_digest,
            ScopedOperationIdentity(grant.tenant_id, grant.operation_id),
            publication_grant=grant.fields(),
        )
        return publication_receipt_to_wire(receipt)

    def get_publication_receipt(self, tenant_id: str, output_set_id: str):
        receipt = self._app.get_publication_receipt(tenant_id, output_set_id)
        return publication_receipt_to_wire(receipt) if receipt is not None else None


def _verification_keys() -> dict[str, bytes]:
    configured = os.environ.get("BC05_GRANT_PUBLIC_KEYS_JSON")
    if configured:
        try:
            encoded = json.loads(configured)
            if not isinstance(encoded, dict) or not encoded:
                raise ValueError
            return {key_id: bytes.fromhex(value) for key_id, value in encoded.items()}
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit("BC05_GRANT_PUBLIC_KEYS_JSON is invalid") from error

    if os.environ.get("BC04_LOCAL_FIXTURE_PROFILE") != "1":
        raise SystemExit(
            "BC05_GRANT_PUBLIC_KEYS_JSON is required outside the local fixture profile"
        )
    # This derivation is permitted only by the explicit local composition. A
    # production BC05 process receives the public-key ring, never this seed.
    try:
        seed = bytes.fromhex(os.environ["BC04_GRANT_SIGNING_KEY"])
        key_id = os.environ["BC04_GRANT_KEY_ID"]
        public = (
            Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
    except (KeyError, ValueError) as error:
        raise SystemExit("local fixture grant key configuration is invalid") from error
    return {key_id: public}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--tenant-id")
    parser.add_argument("--run-id")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be from 1 through 100")
    if not 1 <= args.timeout_seconds <= 300:
        parser.error("--timeout-seconds must be from 1 through 300")
    if (args.tenant_id is None) != (args.run_id is None):
        parser.error("--tenant-id and --run-id must be provided together")

    dsn = os.environ.get("BC04_DATABASE_URL")
    if not dsn:
        raise SystemExit("BC04_DATABASE_URL is required")
    verifier = Ed25519PublicationGrantVerifier(_verification_keys())
    repository = PostgresDurableRunRepository(dsn)
    with MongoResources(MongoSettings.from_env()) as resources:
        store = MongoOutputStore(resources)
        app = AcquisitionOutputApplication(
            store,
            store,
            payload_contracts=_StagingNotConfigured(),
            custody=_StagingNotConfigured(),
            publication_grants=verifier,
        )
        coordinator = DurableExecutionCoordinator(
            repository,
            _BC05PublicationAdapter(app),
            grant_signer=None,
        )
        results = coordinator.reconcile(
            limit=args.limit,
            timeout_seconds=args.timeout_seconds,
            tenant_id=args.tenant_id,
            run_id=args.run_id,
        )

    for result in results:
        suffix = f" reason={result.reason_code}" if result.reason_code else ""
        print(
            f"tenant={result.tenant_id} run={result.run_id} "
            f"reservation={result.reservation_id or '-'} "
            f"reconciliation={result.state.value} run_state={result.run_state}{suffix}"
        )
    print(f"Processed {len(results)} finalization reservation(s).")
    if any(result.state == FinalizationRecoveryState.CONFLICT for result in results):
        return 2
    if any(result.state == FinalizationRecoveryState.UNRESOLVED for result in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
