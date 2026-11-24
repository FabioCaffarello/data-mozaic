"""Synthetic published-output example using the durable MongoDB adapter."""

from __future__ import annotations

import json
from pathlib import Path

from acquisition_output.adapters.mongodb import MongoOutputStore, MongoResources, MongoSettings
from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import OutputSetSpec, PayloadContractRef, ScopedOperationIdentity
from acquisition_output.wire import capture_output_from_wire

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "contracts/acquisition-output/draft/v1/fixtures/valid/record-output.json"


class _FixtureContracts:
    def validate(self, contract: PayloadContractRef, payload: dict[str, object]) -> None:
        if contract.contract_id != "example.catalog.item" or set(payload) != {"name", "price", "active"}:
            raise ValueError("unexpected synthetic catalog fixture")


class _NoCustody:
    def confirm(self, artifact, *, tenant_id, source):
        raise AssertionError("record fixture has no artifact custody dependency")


def _application(resources: MongoResources) -> AcquisitionOutputApplication:
    store = MongoOutputStore(resources)
    return AcquisitionOutputApplication(
        store, store, payload_contracts=_FixtureContracts(), custody=_NoCustody(), outbox=store
    )


def main() -> None:
    output = capture_output_from_wire(json.loads(FIXTURE.read_text(encoding="utf-8")))
    settings = MongoSettings.from_env()
    with MongoResources(settings) as resources:
        app = _application(resources)
        spec = OutputSetSpec(
            output.output_set_id,
            output.binding,
            (output.payload_contract,),
            expected_output_count=1,
        )
        app.create_output_set(spec, ScopedOperationIdentity(output.binding.tenant_id, "example-create"))
        app.stage_output(output)
        sealed = app.seal_output_set(
            output.binding.tenant_id,
            output.output_set_id,
            ScopedOperationIdentity(output.binding.tenant_id, "example-seal"),
        )
        publication = app.publish_output_set(
            output.binding.tenant_id,
            output.output_set_id,
            sealed.manifest_digest,
            ScopedOperationIdentity(output.binding.tenant_id, "example-publish"),
        )

    # A new client demonstrates that the result and operation receipts are durable.
    with MongoResources(settings) as resources:
        app = _application(resources)
        replay = app.stage_output(output)
        page = app.read_published_outputs(output.binding.tenant_id, output.output_set_id)
        stored_receipt = app.get_publication_receipt(output.binding.tenant_id, output.output_set_id)
        events = tuple(
            event for event in app.list_pending_events(output.binding.tenant_id, limit=1000)
            if event["aggregate_id"] == output.output_set_id
        )
        if not page.outputs or replay.output_id != output.identity.output_id:
            raise SystemExit("durable published output or stage replay was not found")
        if stored_receipt is None or stored_receipt.publication_id != publication.publication_id:
            raise SystemExit("durable publication receipt was not found")
        if len(events) != 1:
            raise SystemExit("expected exactly one pending publication event")
        view = {
            "evidence": "synthetic repository fixture; not a live source observation",
            "output_set_id": output.output_set_id,
            "state": app.inspect_output_set(output.binding.tenant_id, output.output_set_id).state.value,
            "manifest_digest": sealed.manifest_digest,
            "replayed_operation_id": replay.operation_id,
            "publication_id": stored_receipt.publication_id,
            "pending_event_id": events[0]["event_id"],
            "record_count": len(page.outputs),
            "record": {
                "natural_key": page.outputs[0].identity.natural_key,
                "name": page.outputs[0].payload["name"],
                "price": page.outputs[0].payload["price"],
                "active": page.outputs[0].payload["active"],
            },
        }
        print(json.dumps(view, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
