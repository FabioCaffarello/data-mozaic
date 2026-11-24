"""Synthetic example of a consumer that reads published BC05 outputs."""

from __future__ import annotations

import json
from pathlib import Path

from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import OutputSetSpec, PayloadContractRef, ScopedOperationIdentity
from acquisition_output.memory import InMemoryOutputStore
from acquisition_output.wire import capture_output_from_wire


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "contracts/acquisition-output/draft/v1/fixtures/valid/record-output.json"
EXPECTATION = Path(__file__).with_name("expected-consumer-view.json")


class FixturePayloadContracts:
    """Independent expected fields for the synthetic catalog contract."""

    def validate(self, contract: PayloadContractRef, payload) -> None:
        if (contract.contract_id, contract.version) != ("example.catalog.item", "1.0.0"):
            raise ValueError("unexpected payload contract")
        if set(payload) != {"name", "price", "active"}:
            raise ValueError("unexpected synthetic record fields")


class NoArtifactCustodyRequired:
    def confirm(self, artifact, *, tenant_id, source):
        raise AssertionError("record-only fixture must not request artifact custody")


def main() -> None:
    wire = json.loads(FIXTURE.read_text(encoding="utf-8"))
    output = capture_output_from_wire(wire)
    store = InMemoryOutputStore()
    application = AcquisitionOutputApplication(
        store,
        store,
        payload_contracts=FixturePayloadContracts(),
        custody=NoArtifactCustodyRequired(),
    )
    spec = OutputSetSpec(
        output_set_id=output.output_set_id,
        binding=output.binding,
        payload_contracts=(output.payload_contract,),
        expected_output_count=1,
    )
    application.create_output_set(spec, ScopedOperationIdentity(output.binding.tenant_id, "example-create"))
    application.stage_output(output)
    sealed = application.seal_output_set(
        output.binding.tenant_id,
        output.output_set_id,
        ScopedOperationIdentity(output.binding.tenant_id, "example-seal"),
    )
    application.publish_output_set(
        output.binding.tenant_id,
        output.output_set_id,
        sealed.manifest_digest,
        ScopedOperationIdentity(output.binding.tenant_id, "example-publish"),
    )

    published = application.inspect_output_set(output.binding.tenant_id, output.output_set_id)
    page = application.read_published_outputs(output.binding.tenant_id, output.output_set_id)
    view = {
        "output_set_id": published.output_set_id,
        "state": published.state.value,
        "records": [
            {
                "natural_key": item.identity.natural_key,
                "name": item.payload["name"],
                "price": item.payload["price"],
                "active": item.payload["active"],
                "observed_at": item.identity.observed_at.isoformat(),
            }
            for item in page.outputs
        ],
    }
    expected = json.loads(EXPECTATION.read_text(encoding="utf-8"))
    if view != expected:
        raise SystemExit(f"published fixture output differed from independent expectation: {view!r}")
    print(json.dumps(view, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
