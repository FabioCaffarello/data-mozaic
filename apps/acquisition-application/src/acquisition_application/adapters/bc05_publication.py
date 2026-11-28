"""BC04 finalization adapter that delegates publication to BC05."""

from acquisition_output.domain import ScopedOperationIdentity
from acquisition_output.wire import publication_receipt_to_wire


class BC05PublicationAdapter:
    def __init__(self, outputs) -> None:
        self._outputs = outputs

    def publish_grant(self, grant):
        receipt = self._outputs.publish_output_set(
            grant.tenant_id,
            grant.output_set_id,
            grant.manifest_digest,
            ScopedOperationIdentity(grant.tenant_id, grant.operation_id),
            publication_grant=grant.fields(),
        )
        return publication_receipt_to_wire(receipt)

    def get_publication_receipt(self, tenant_id: str, output_set_id: str):
        receipt = self._outputs.get_publication_receipt(tenant_id, output_set_id)
        return publication_receipt_to_wire(receipt) if receipt is not None else None
