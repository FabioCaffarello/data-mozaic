"""Configuration-backed operator interface for bounded acquisition runs."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import rfc8785
from acquisition_lifecycle.cli import compose_for_operations
from acquisition_output.adapters.http_custody import ArtifactCustodyHTTPAdapter
from acquisition_output.adapters.mongodb import MongoOutputStore, MongoResources
from acquisition_output.adapters.mongodb.config import MongoSettings
from acquisition_output.adapters.publication_grant import (
    Ed25519PublicationGrantVerifier,
)
from acquisition_output.application import AcquisitionOutputApplication
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.execution_capability import (
    ExecutionCapabilitySigner,
)
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from execution_coordination.adapters.publication_grant import (
    Ed25519PublicationGrantSigner,
)
from execution_coordination.adapters.trusted_live_authority import TrustedLiveAuthority
from execution_coordination.durable_application import DurableExecutionCoordinator
from mozaic_contract_resolution import ContractReference, ImmutableContractRegistry
from work_intake.adapters.composition import compose_work_intake
from work_intake.domain import LogicalInput

from .adapters.artifact_custody import ArtifactCustodyHTTPClient
from .adapters.bc05_publication import BC05PublicationAdapter
from .adapters.protected_live_worker import DockerComposeProtectedLiveWorker
from .adapters.trusted_live_import import TrustedLiveAttemptImporter
from .application import (
    ExactJSONSchemaPayloadContractValidator,
    TrustedAcquisitionApplication,
)
from .operational import OperationalAcquisition

ROOT = Path(__file__).resolve().parents[4]
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required configuration is missing: {name}")
    return value


def _key_file(name: str, *, private: bool) -> bytes:
    path = Path(_required(name))
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError(f"{name} must identify a bounded regular file")
    if private and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError(f"{name} permissions must be owner-only")
    value = path.read_bytes()
    if len(value) != 32:
        raise ValueError(f"{name} must contain exactly 32 raw bytes")
    return value


def _input_document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
        raise ValueError("Input document must be a bounded regular file")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Input document contains duplicate properties")
            result[key] = value
        return result

    document = json.loads(path.read_bytes(), object_pairs_hook=unique)
    required = {
        "tenant_id",
        "input_id",
        "source_id",
        "source_revision_id",
        "input_contract",
        "idempotency_scope",
        "idempotency_key",
        "release_id",
        "parameters",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise ValueError("Input document fields do not match the operator contract")
    for key in required - {"input_contract", "parameters"}:
        if not isinstance(document[key], str) or not _ID.fullmatch(document[key]):
            raise ValueError(f"Input document identity is invalid: {key}")
    contract = document["input_contract"]
    if not isinstance(contract, dict) or set(contract) != {
        "contract_id",
        "version",
        "digest",
    }:
        raise ValueError("Input Contract reference is invalid")
    if not isinstance(document["parameters"], dict):
        raise TypeError("Input parameters must be an object")
    return document


def _submission_json(value) -> dict[str, object]:
    return {
        "submission_id": value.intent.submission_id,
        "tenant_id": value.intent.tenant_id,
        "input_id": value.intent.input_id,
        "release_id": value.intent.release_id,
        "parameters_digest": value.intent.parameters_digest,
        "state": value.state.value,
        "run_id": value.run_id,
        "diagnostic_code": value.diagnostic_code,
    }


@contextmanager
def _runtime():
    required = (
        "BC04_DATABASE_URL",
        "WORK_INTAKE_DATABASE_URL",
        "BC04_GRANT_SIGNING_KEY",
        "BC04_GRANT_KEY_ID",
        "ARTIFACT_CUSTODY_HTTP_URL",
        "ARTIFACT_CUSTODY_LOCAL_TOKEN",
        "GATEWAY_HTTP_URL",
        "GATEWAY_EVIDENCE_IMPORT_TOKEN",
        "MOZAIC_OPERATOR_ID",
        "MOZAIC_AUTHORITY_ID",
        "MOZAIC_AUTHORITY_TRUSTED_PUBLIC_KEY_FILE",
        "MOZAIC_EXECUTION_CAPABILITY_SIGNING_KEY_FILE",
        "MOZAIC_EXECUTION_CAPABILITY_KEY_ID",
        "MOZAIC_LIVE_COMPOSE_PROJECT",
        "MOZAIC_LIVE_TRUSTED_CONFIG",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError("required configuration is missing: " + ", ".join(missing))
    actor = os.environ["MOZAIC_OPERATOR_ID"]
    if not _ID.fullmatch(actor):
        raise ValueError("MOZAIC_OPERATOR_ID is invalid")

    lifecycle, lifecycle_store = compose_for_operations()
    repository = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"])
    envelope = repository.current_authority_envelope(os.environ["MOZAIC_AUTHORITY_ID"])
    if envelope is None or not isinstance(envelope.get("key_id"), str):
        raise ValueError("BC04 has no current signed acquisition authority")
    authority = TrustedLiveAuthority(
        rfc8785.dumps(envelope),
        trusted_public_keys={
            envelope["key_id"]: _key_file(
                "MOZAIC_AUTHORITY_TRUSTED_PUBLIC_KEY_FILE", private=False
            )
        },
        minimum_generation=1,
        authority_registry=repository,
    )
    grant_seed = bytes.fromhex(os.environ["BC04_GRANT_SIGNING_KEY"])
    if len(grant_seed) != 32:
        raise ValueError("BC04_GRANT_SIGNING_KEY must encode 32 bytes")
    grant_key_id = os.environ["BC04_GRANT_KEY_ID"]
    grant_public = (
        Ed25519PrivateKey.from_private_bytes(grant_seed).public_key().public_bytes_raw()
    )
    capability_seed = _key_file(
        "MOZAIC_EXECUTION_CAPABILITY_SIGNING_KEY_FILE", private=True
    )
    capability_key_id = os.environ["MOZAIC_EXECUTION_CAPABILITY_KEY_ID"]
    capability_signer = ExecutionCapabilitySigner(
        capability_seed, key_id=capability_key_id
    )
    gateway_keys_path = Path(os.environ["MOZAIC_LIVE_TRUSTED_CONFIG"]) / "keys.json"
    if gateway_keys_path.is_symlink() or not gateway_keys_path.is_file():
        raise ValueError("Gateway trusted key configuration is unavailable")
    gateway_keys = json.loads(gateway_keys_path.read_bytes())
    if not isinstance(gateway_keys, dict):
        raise TypeError("Gateway trusted key document is invalid")
    gateway_public = (
        Ed25519PrivateKey.from_private_bytes(capability_seed)
        .public_key()
        .public_bytes_raw()
    )
    configured_key = gateway_keys.get(capability_key_id)
    try:
        configured_public = base64.urlsafe_b64decode(
            configured_key + "=" * (-len(configured_key) % 4)
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Gateway does not trust the configured BC04 key") from error
    if configured_public != gateway_public:
        raise ValueError("Gateway trusted key differs from the BC04 capability key")
    contract_root = Path(
        os.environ.get(
            "MOZAIC_PAYLOAD_CONTRACT_ROOT", ROOT / "contracts/payload/draft/v1"
        )
    )
    contracts = ImmutableContractRegistry(contract_root)
    custody_url = os.environ["ARTIFACT_CUSTODY_HTTP_URL"]
    custody_token = os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"]

    with MongoResources(MongoSettings.from_env()) as resources:
        store = MongoOutputStore(resources)
        outputs = AcquisitionOutputApplication(
            store,
            store,
            payload_contracts=ExactJSONSchemaPayloadContractValidator(contracts),
            custody=ArtifactCustodyHTTPAdapter(custody_url, custody_token),
            outbox=store,
            publication_grants=Ed25519PublicationGrantVerifier(
                {grant_key_id: grant_public}
            ),
        )
        coordinator = DurableExecutionCoordinator(
            repository,
            BC05PublicationAdapter(outputs),
            Ed25519PublicationGrantSigner(grant_seed, key_id=grant_key_id),
            lease_seconds=120,
            live_authority=authority,
            execution_capability_signer=capability_signer,
            execution_capability_verifier=capability_signer,
        )
        intake = compose_work_intake(
            database_url=os.environ["WORK_INTAKE_DATABASE_URL"],
            migrations_root=ROOT / "migrations",
            contract_root=contract_root,
            lifecycle=lifecycle,
            coordinator=coordinator,
        )
        trusted = TrustedAcquisitionApplication(
            repository,
            outputs,
            ArtifactCustodyHTTPClient(custody_url, custody_token),
            finalization=coordinator,
        )
        operation = OperationalAcquisition(
            work_intake=intake,
            coordinator=coordinator,
            protected_attempt=DockerComposeProtectedLiveWorker(
                compose_project=os.environ["MOZAIC_LIVE_COMPOSE_PROJECT"]
            ),
            trusted_import=TrustedLiveAttemptImporter(
                acquisition=trusted,
                lifecycle=lifecycle,
                lifecycle_store=lifecycle_store,
                gateway_url=os.environ["GATEWAY_HTTP_URL"],
                gateway_import_token=os.environ["GATEWAY_EVIDENCE_IMPORT_TOKEN"],
            ),
        )
        yield operation, coordinator


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acquisition-operator")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("submit", "run"):
        command = commands.add_parser(name)
        command.add_argument("--document", type=Path, required=True)
    command = commands.add_parser("dispatch")
    command.add_argument("--tenant-id", required=True)
    command.add_argument("--limit", type=int, default=10)
    for name in ("inspect", "cancel", "execute", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("--tenant-id", required=True)
        command.add_argument("--submission-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        with _runtime() as (operation, coordinator):
            actor = os.environ["MOZAIC_OPERATOR_ID"]
            if args.command in {"submit", "run"}:
                doc = _input_document(args.document)
                contract = doc["input_contract"]
                submission = operation.submit(
                    logical_input=LogicalInput(
                        doc["tenant_id"],
                        doc["input_id"],
                        doc["source_id"],
                        doc["source_revision_id"],
                        ContractReference(
                            contract["contract_id"],
                            contract["version"],
                            contract["digest"],
                        ),
                    ),
                    idempotency_scope=doc["idempotency_scope"],
                    idempotency_key=doc["idempotency_key"],
                    release_id=doc["release_id"],
                    parameters=doc["parameters"],
                )
                if args.command == "run":
                    operation.dispatch(
                        worker_id=actor, limit=1, tenant_id=doc["tenant_id"]
                    )
                    submission = operation.inspect(
                        doc["tenant_id"], submission.intent.submission_id
                    )["submission"]
                    if submission.run_id is None:
                        raise RuntimeError(
                            "BC13 dispatch did not produce BC04 admission: "
                            + str(submission.diagnostic_code)
                        )
                    result = operation.execute(
                        tenant_id=doc["tenant_id"],
                        submission_id=submission.intent.submission_id,
                        worker_id=actor,
                    )
                    print(
                        json.dumps(
                            {
                                "submission": _submission_json(submission),
                                "execution": {
                                    "state": result["state"],
                                    "replayed": result["replayed"],
                                    "run_id": result["run"].intent.run_id,
                                    "publication_receipt": result[
                                        "run"
                                    ].publication_receipt,
                                },
                            },
                            sort_keys=True,
                        )
                    )
                else:
                    print(json.dumps(_submission_json(submission), sort_keys=True))
            elif args.command == "dispatch":
                if not 1 <= args.limit <= 100:
                    raise ValueError("--limit must be from 1 through 100")
                result = operation.dispatch(
                    worker_id=actor, limit=args.limit, tenant_id=args.tenant_id
                )
                print(
                    json.dumps(
                        [_submission_json(item) for item in result], sort_keys=True
                    )
                )
            elif args.command == "inspect":
                submission = operation.inspect(args.tenant_id, args.submission_id)
                execution = submission["execution"]
                print(
                    json.dumps(
                        {
                            "submission": _submission_json(submission["submission"]),
                            "execution_state": execution.state.value,
                            "publication_receipt": execution.publication_receipt,
                        },
                        sort_keys=True,
                    )
                )
            elif args.command == "cancel":
                print(
                    json.dumps(
                        _submission_json(
                            operation.cancel(args.tenant_id, args.submission_id)
                        ),
                        sort_keys=True,
                    )
                )
            elif args.command == "execute":
                result = operation.execute(
                    tenant_id=args.tenant_id,
                    submission_id=args.submission_id,
                    worker_id=actor,
                )
                print(
                    json.dumps(
                        {
                            "state": result["state"],
                            "replayed": result["replayed"],
                            "run_id": result["run"].intent.run_id,
                            "publication_receipt": result["run"].publication_receipt,
                        },
                        sort_keys=True,
                    )
                )
            else:
                claims = operation.reconcile(
                    worker_id=actor, limit=10, tenant_id=args.tenant_id
                )
                submission = operation.inspect(args.tenant_id, args.submission_id)[
                    "submission"
                ]
                if submission.run_id:
                    run = coordinator.inspect(args.tenant_id, submission.run_id)
                    if run.reservation is not None:
                        coordinator.reconcile_finalization(
                            args.tenant_id, submission.run_id
                        )
                print(
                    json.dumps(
                        {
                            "submission": _submission_json(submission),
                            "reconciled_claims": len(claims),
                        },
                        sort_keys=True,
                    )
                )
        return 0
    except Exception as error:  # noqa: BLE001 - return owner diagnostics without secret configuration.
        reason = str(error)
        for name in (
            "BC04_DATABASE_URL",
            "WORK_INTAKE_DATABASE_URL",
            "BC04_GRANT_SIGNING_KEY",
            "ARTIFACT_CUSTODY_LOCAL_TOKEN",
            "GATEWAY_EVIDENCE_IMPORT_TOKEN",
        ):
            secret = os.environ.get(name)
            if secret:
                reason = reason.replace(secret, "[redacted]")
        print(json.dumps({"state": "blocked", "reason": reason}, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
