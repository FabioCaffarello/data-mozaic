"""BC05 output use cases; no database, worker or transport dependencies."""

from __future__ import annotations

import base64
from collections.abc import Mapping

from .domain import (
    CaptureOutputVariant,
    CompositeOutput,
    OutputError,
    OutputSetSnapshot,
    OutputSetSpec,
    PublicationReceipt,
    PublishedOutputPage,
    RecordOutput,
    ScopedOperationIdentity,
    canonical_json,
    output_intent_digest,
    output_set_intent_digest,
    plain_json,
)
from .ports import (
    ArtifactCustodyPort,
    OutputSetPublicationPort,
    OutputSetRepositoryPort,
    PayloadContractValidationPort,
    PendingOutboxEventPort,
    PublicationGrantVerificationPort,
)


class AcquisitionOutputApplication:
    """Create/stage/seal/publish output sets through BC05-owned ports."""

    def __init__(
        self,
        repository: OutputSetRepositoryPort,
        publisher: OutputSetPublicationPort,
        *,
        payload_contracts: PayloadContractValidationPort,
        custody: ArtifactCustodyPort,
        outbox: PendingOutboxEventPort | None = None,
        publication_grants: PublicationGrantVerificationPort | None = None,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._payload_contracts = payload_contracts
        self._custody = custody
        self._outbox = outbox
        self._publication_grants = publication_grants

    def create_output_set(
        self, spec: OutputSetSpec, operation: ScopedOperationIdentity
    ) -> OutputSetSnapshot:
        self._require_scope(operation, spec.binding.tenant_id)
        manifest = self._repository.create(spec, operation)
        if (
            manifest.output_set_id != spec.output_set_id
            or manifest.binding != spec.binding
        ):
            raise OutputError(
                "repository returned a manifest for a different OutputSet"
            )
        return manifest

    def stage_output(self, output: CaptureOutputVariant):
        intent_digest = output_intent_digest(output)
        previous = self._repository.find_stage_receipt(output.operation, intent_digest)
        if previous is not None:
            if (
                previous.tenant_id != output.binding.tenant_id
                or previous.operation_id != output.operation.operation_id
                or previous.output_set_id != output.output_set_id
                or previous.output_id != output.identity.output_id
                or previous.intent_digest != intent_digest
            ):
                raise OutputError(
                    "repository returned an idempotency receipt for different intent"
                )
            return previous
        if isinstance(output, (RecordOutput, CompositeOutput)):
            self._payload_contracts.validate(
                output.payload_contract,
                plain_json(output.payload),
            )
        confirmed = []
        for item in output.artifact_items:
            receipt = self._custody.confirm(
                item.artifact,
                tenant_id=output.binding.tenant_id,
                source=output.binding.source,
            )
            if (
                receipt.tenant_id != output.binding.tenant_id
                or receipt.artifact_id != item.artifact.artifact_id
                or receipt.digest != item.artifact.digest
                or receipt.size_bytes != item.artifact.size_bytes
            ):
                raise OutputError(
                    "custody confirmation does not match artifact reference and tenant"
                )
            confirmed.append(receipt)
        receipt = self._repository.stage(output, intent_digest, tuple(confirmed))
        if (
            receipt.tenant_id != output.binding.tenant_id
            or receipt.operation_id != output.operation.operation_id
            or receipt.output_set_id != output.output_set_id
            or receipt.output_id != output.identity.output_id
            or receipt.intent_digest != intent_digest
        ):
            raise OutputError(
                "repository returned an OutputReceipt for different intent"
            )
        return receipt

    def seal_output_set(
        self, tenant_id: str, output_set_id: str, operation: ScopedOperationIdentity
    ) -> OutputSetSnapshot:
        self._require_scope(operation, tenant_id)
        manifest = self._repository.seal(tenant_id, output_set_id, operation)
        if (
            manifest.output_set_id != output_set_id
            or manifest.binding.tenant_id != tenant_id
            or manifest.state.value not in {"sealed", "published"}
        ):
            raise OutputError(
                "repository returned an invalid sealed OutputSet manifest"
            )
        return manifest

    def publish_output_set(
        self,
        tenant_id: str,
        output_set_id: str,
        manifest_digest: str,
        operation: ScopedOperationIdentity,
        *,
        publication_grant: Mapping[str, object] | None = None,
    ) -> PublicationReceipt:
        self._require_scope(operation, tenant_id)
        snapshot = self._repository.inspect(tenant_id, output_set_id)
        if snapshot is None:
            raise OutputError("OutputSet does not exist")
        execution = snapshot.binding.execution
        authorization = None
        if execution.run_id is not None:
            if publication_grant is None or self._publication_grants is None:
                raise OutputError(
                    "run-bound OutputSet publication requires a BC04 grant"
                )
            authorization = self._publication_grants.verify(publication_grant)
            if (
                authorization.tenant_id != tenant_id
                or authorization.run_id != execution.run_id
                or authorization.attempt_id != execution.attempt_id
                or authorization.fencing_generation != execution.fencing_token
                or authorization.output_set_id != output_set_id
                or authorization.manifest_digest != manifest_digest
                or authorization.operation_id != operation.operation_id
            ):
                raise OutputError(
                    "BC04 publication grant does not match sealed OutputSet"
                )
        elif publication_grant is not None:
            raise OutputError(
                "unbound OutputSet cannot consume a BC04 publication grant"
            )
        receipt = self._publisher.publish(
            tenant_id, output_set_id, manifest_digest, operation, authorization
        )
        if (
            receipt.tenant_id != tenant_id
            or receipt.output_set_id != output_set_id
            or receipt.manifest_digest != manifest_digest
            or receipt.finalization_reservation_id
            != (authorization.reservation_id if authorization else None)
        ):
            raise OutputError(
                "publisher returned a receipt for different OutputSet intent"
            )
        return receipt

    def get_publication_receipt(
        self, tenant_id: str, output_set_id: str
    ) -> PublicationReceipt | None:
        return self._publisher.get_publication_receipt(tenant_id, output_set_id)

    def list_pending_events(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[object, ...]:
        if self._outbox is None:
            raise OutputError("pending outbox inspection is not configured")
        if not 1 <= limit <= 1000:
            raise OutputError("pending outbox page size must be from 1 through 1000")
        return self._outbox.list_pending_events(tenant_id, limit=limit)

    def abort_output_set(
        self,
        tenant_id: str,
        output_set_id: str,
        operation: ScopedOperationIdentity,
        reason: str,
    ) -> OutputSetSnapshot:
        self._require_scope(operation, tenant_id)
        return self._repository.abort(tenant_id, output_set_id, operation, reason)

    def reject_output_set(
        self,
        tenant_id: str,
        output_set_id: str,
        operation: ScopedOperationIdentity,
        reason: str,
    ) -> OutputSetSnapshot:
        self._require_scope(operation, tenant_id)
        return self._repository.reject(tenant_id, output_set_id, operation, reason)

    def inspect_output_set(
        self, tenant_id: str, output_set_id: str
    ) -> OutputSetSnapshot | None:
        return self._repository.inspect(tenant_id, output_set_id)

    def read_published_outputs(
        self,
        tenant_id: str,
        output_set_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> PublishedOutputPage:
        if not 1 <= limit <= 1000:
            raise OutputError("published output page size must be from 1 through 1000")
        after_output_id = self._decode_cursor(cursor, tenant_id, output_set_id)
        rows = self._repository.read_published(
            tenant_id,
            output_set_id,
            after_output_id=after_output_id,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        outputs = rows[:limit]
        next_cursor = (
            self._encode_cursor(
                tenant_id, output_set_id, outputs[-1].identity.output_id
            )
            if has_more and outputs
            else None
        )
        return PublishedOutputPage(outputs, next_cursor)

    @staticmethod
    def _encode_cursor(tenant_id: str, output_set_id: str, after_output_id: str) -> str:
        document = canonical_json(
            {
                "tenant_id": tenant_id,
                "output_set_id": output_set_id,
                "after_output_id": after_output_id,
            }
        )
        return base64.urlsafe_b64encode(document).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(
        cursor: str | None, tenant_id: str, output_set_id: str
    ) -> str | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise OutputError("published output cursor is invalid or too large")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            document = base64.urlsafe_b64decode(padded.encode("ascii"))
            from .domain import strict_json_loads

            value = strict_json_loads(document)
        except (ValueError, UnicodeEncodeError) as error:
            raise OutputError("published output cursor is malformed") from error
        if (
            not isinstance(value, dict)
            or set(value) != {"tenant_id", "output_set_id", "after_output_id"}
            or value["tenant_id"] != tenant_id
            or value["output_set_id"] != output_set_id
            or not isinstance(value["after_output_id"], str)
        ):
            raise OutputError("published output cursor belongs to another query")
        return value["after_output_id"]

    @staticmethod
    def _require_scope(operation: ScopedOperationIdentity, tenant_id: str) -> None:
        if operation.tenant_id != tenant_id:
            raise OutputError("operation tenant does not match the requested tenant")


def create_intent_digest(spec: OutputSetSpec) -> str:
    """Compatibility-friendly application name for the domain intent digest."""
    return output_set_intent_digest(spec)
