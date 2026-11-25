"""Synthetic durable BC04 to BC05 publication and lost-ack recovery flow."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.request import Request, urlopen

from acquisition_output.adapters.http_custody import ArtifactCustodyHTTPAdapter
from acquisition_output.adapters.mongodb import MongoOutputStore, MongoResources
from acquisition_output.adapters.mongodb.config import MongoSettings
from acquisition_output.adapters.publication_grant import (
    Ed25519PublicationGrantVerifier,
)
from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import (
    ArtifactItem,
    ArtifactOutput,
    ArtifactRef,
    OutputIdentity,
    OutputSetBinding,
    OutputSetSpec,
    PayloadContractRef,
    ProducerIdentity,
    ScopedOperationIdentity,
    SourceRevisionRef,
)
from acquisition_output.domain import (
    ExecutionBinding as OutputExecutionBinding,
)
from acquisition_output.wire import publication_receipt_to_wire
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from execution_coordination.adapters.publication_grant import (
    Ed25519PublicationGrantSigner,
)
from execution_coordination.domain import ExecutionBinding
from execution_coordination.durable_application import (
    DurableExecutionCoordinator,
)
from execution_coordination.durable_domain import DurableRunIntent


class FixtureContractValidator:
    def __init__(self, contract: PayloadContractRef) -> None:
        self._contract = contract

    def validate(self, contract: PayloadContractRef, payload) -> None:
        if contract != self._contract or set(payload) != {"value"}:
            raise ValueError("synthetic fixture payload does not match its contract")


def custody_request(path: str, *, method: str, body: bytes, tenant: str, token: str):
    request = Request(
        os.environ["ARTIFACT_CUSTODY_HTTP_URL"].rstrip("/") + path,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "X-DM-Tenant-ID": tenant},
    )
    with urlopen(request, timeout=30) as response:
        return response.read()


class Bc05Publisher:
    def __init__(
        self, app: AcquisitionOutputApplication, *, lose_next_ack: bool = False
    ):
        self._app = app
        self._lose_next_ack = lose_next_ack

    def publish_grant(self, grant):
        receipt = self._app.publish_output_set(
            grant.tenant_id,
            grant.output_set_id,
            grant.manifest_digest,
            ScopedOperationIdentity(grant.tenant_id, grant.operation_id),
            publication_grant=grant.fields(),
        )
        if self._lose_next_ack:
            self._lose_next_ack = False
            raise TimeoutError("synthetic lost acknowledgment after MongoDB commit")
        return publication_receipt_to_wire(receipt)

    def get_publication_receipt(self, tenant_id: str, output_set_id: str):
        receipt = self._app.get_publication_receipt(tenant_id, output_set_id)
        return publication_receipt_to_wire(receipt) if receipt is not None else None


def main() -> None:
    dsn = os.environ["BC04_DATABASE_URL"]
    signing_key = bytes.fromhex(os.environ["BC04_GRANT_SIGNING_KEY"])
    key_id = os.environ["BC04_GRANT_KEY_ID"]
    tenant = "tenant-synthetic-finalization"
    suffix = uuid.uuid4().hex
    run_id = str(uuid.uuid4())
    contract = PayloadContractRef(
        "synthetic.finalization.record", "1.0.0", "sha256:" + "1" * 64
    )
    execution = PostgresDurableRunRepository(dsn)
    binding = ExecutionBinding(
        source_id="synthetic-source",
        source_revision="fixture-v1",
        bot_id="bot-" + "a" * 64,
        build_id="a" * 64,
        bot_build_manifest_digest="a" * 64,
        assembly_id="a" * 64,
        catalog_digest="a" * 64,
        oci_image="registry.invalid/synthetic@sha256:" + "a" * 64,
        oci_digest="sha256:" + "a" * 64,
        local_image_id="sha256:" + "a" * 64,
        fixture_bundle_id="synthetic-bundle",
        fixture_bundle_digest="a" * 64,
    )
    execution.create(
        DurableRunIntent(
            tenant,
            run_id,
            "request-" + suffix,
            "create-" + suffix,
            binding,
            datetime.now(UTC) + timedelta(minutes=2),
        )
    )
    lease = execution.claim(
        tenant,
        run_id,
        "fixture-only-worker",
        "token-" + suffix,
        "claim-" + suffix,
        30,
    )
    output_set_id = "set-" + suffix
    output_binding = OutputSetBinding(
        tenant,
        SourceRevisionRef("synthetic-source", "fixture-v1"),
        ProducerIdentity("synthetic-fixture-consumer", "1.0.0"),
        OutputExecutionBinding(run_id, lease.attempt_id, lease.generation),
    )
    operation = ScopedOperationIdentity
    artifact_id = "artifact-" + suffix
    artifact_operation = "artifact-op-" + suffix
    artifact_bytes = (
        "synthetic fixture artifact; not source evidence\n" * 128
    ).encode()
    artifact_digest = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    artifact = ArtifactRef(
        artifact_id, artifact_digest, len(artifact_bytes), "application/octet-stream"
    )
    custody_token = os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"]
    custody_intent = {
        "contract_version": "artifact-custody.v1",
        "operation_id": artifact_operation,
        "tenant_id": tenant,
        "artifact_id": artifact_id,
        "source": {"source_id": "synthetic-source", "revision": "fixture-v1"},
        "artifact": {
            "artifact_id": artifact_id,
            "digest": artifact_digest,
            "size_bytes": len(artifact_bytes),
            "media_type": "application/octet-stream",
        },
        "representation": "downloaded_file_bytes",
        "classification": "internal",
        "retention_policy_ref": "local-fixture-v1",
    }
    custody_request(
        "/draft/v1/artifact-intents",
        method="POST",
        body=json.dumps(custody_intent).encode(),
        tenant=tenant,
        token=custody_token,
    )
    custody_request(
        f"/draft/v1/artifact-intents/{artifact_operation}/content",
        method="PUT",
        body=artifact_bytes,
        tenant=tenant,
        token=custody_token,
    )

    def make_app(store: MongoOutputStore) -> AcquisitionOutputApplication:
        public_key = (
            Ed25519PrivateKey.from_private_bytes(signing_key)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
        verifier = Ed25519PublicationGrantVerifier({key_id: public_key})
        return AcquisitionOutputApplication(
            store,
            store,
            payload_contracts=FixtureContractValidator(contract),
            custody=ArtifactCustodyHTTPAdapter(
                os.environ["ARTIFACT_CUSTODY_HTTP_URL"], custody_token
            ),
            outbox=store,
            publication_grants=verifier,
        )

    resource = MongoResources(MongoSettings.from_env())
    with resource:
        store = MongoOutputStore(resource)
        app = make_app(store)
        app.create_output_set(
            OutputSetSpec(output_set_id, output_binding, (), 1),
            operation(tenant, "create-set-" + suffix),
        )
        output = ArtifactOutput(
            identity=OutputIdentity(
                "output-" + suffix, "observation-" + suffix, datetime.now(UTC)
            ),
            operation=operation(tenant, "stage-" + suffix),
            output_set_id=output_set_id,
            binding=output_binding,
            artifacts=(ArtifactItem(artifact),),
        )
        stage_receipt = app.stage_output(output)
        sealed = app.seal_output_set(
            tenant, output_set_id, operation(tenant, "seal-" + suffix)
        )
        publisher = Bc05Publisher(app, lose_next_ack=True)
        coordinator = DurableExecutionCoordinator(
            execution,
            publisher,
            Ed25519PublicationGrantSigner(signing_key, key_id=key_id),
        )
        try:
            coordinator.reserve_and_publish(
                lease, output_set_id, sealed.manifest_digest, "publish-" + suffix
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("lost-ack failure injection did not run")
        pending = execution.get(tenant, run_id)
        if pending.state.value != "finalization_pending" or pending.reservation is None:
            raise AssertionError("durable reservation was not preserved after lost ACK")

    # Fresh application and MongoDB clients simulate coordinator/client restart.
    def reconcile_with_fresh_clients():
        with MongoResources(MongoSettings.from_env()) as fresh_resources:
            fresh_store = MongoOutputStore(fresh_resources)
            fresh_app = make_app(fresh_store)
            coordinator = DurableExecutionCoordinator(
                PostgresDurableRunRepository(dsn),
                Bc05Publisher(fresh_app),
                grant_signer=None,
            )
            return coordinator.reconcile(
                limit=1,
                timeout_seconds=30,
                tenant_id=tenant,
                run_id=run_id,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery_futures = [
            pool.submit(reconcile_with_fresh_clients),
            pool.submit(reconcile_with_fresh_clients),
        ]
        recoveries = [future.result(timeout=45) for future in recovery_futures]
    recovered_items = [item for items in recoveries for item in items]
    if not recovered_items or any(
        item.state.value != "completed" for item in recovered_items
    ):
        raise AssertionError(
            f"unexpected concurrent reconciliation results: {recoveries!r}"
        )
    with MongoResources(MongoSettings.from_env()) as fresh_resources:
        fresh_store = MongoOutputStore(fresh_resources)
        fresh_app = make_app(fresh_store)
        fresh_publisher = Bc05Publisher(fresh_app)
        fresh_coordinator = DurableExecutionCoordinator(
            PostgresDurableRunRepository(dsn),
            fresh_publisher,
            Ed25519PublicationGrantSigner(signing_key, key_id=key_id),
        )
        completed = PostgresDurableRunRepository(dsn).get(tenant, run_id)
        if completed is None or completed.state.value != "succeeded":
            raise AssertionError("reconciliation did not complete the published run")
        page = fresh_app.read_published_outputs(tenant, output_set_id, limit=10)
        if (
            len(page.outputs) != 1
            or page.outputs[0].identity.output_id != output.identity.output_id
        ):
            raise AssertionError(
                "published fixture output could not be read after restart"
            )
        downloaded = custody_request(
            f"/draft/v1/artifacts/{artifact_id}/content",
            method="GET",
            body=b"",
            tenant=tenant,
            token=custody_token,
        )
        if (
            downloaded != artifact_bytes
            or hashlib.sha256(downloaded).hexdigest() != artifact_digest[7:]
        ):
            raise AssertionError(
                "BC06 returned bytes that differ from the custody digest"
            )
        if completed.publication_receipt is None:
            raise AssertionError("durable publication receipt is missing")
        events = fresh_app.list_pending_events(tenant, limit=100)
        if not any(event.get("aggregate_id") == output_set_id for event in events):
            raise AssertionError("BC05 publication outbox event is missing")
        replay = fresh_coordinator.reserve_and_publish(
            lease, output_set_id, sealed.manifest_digest, "publish-" + suffix
        )
        if (
            replay[1]["publication_id"]
            != completed.publication_receipt["publication_id"]
        ):
            raise AssertionError("publication replay changed durable identity")
        print(
            "Synthetic BC04/PostgreSQL → BC05/MongoDB finalization passed: "
            f"run={run_id}, output_set={output_set_id}, stage_receipt={stage_receipt.status}, "
            f"manifest={sealed.manifest_digest}, output_count={len(page.outputs)}, "
            f"reconciliation_clients={len(recoveries)}, pending_events={len(events)}"
        )


if __name__ == "__main__":
    main()
