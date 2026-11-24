"""Consumer-owned BC05 persistence, publication, validation and custody ports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from .domain import (
    ArtifactRef,
    CaptureOutputVariant,
    CustodyReceipt,
    OutputReceipt,
    OutputSetSnapshot,
    OutputSetSpec,
    PayloadContractRef,
    PublicationReceipt,
    ScopedOperationIdentity,
    SourceRevisionRef,
)


class OutputSetRepositoryPort(Protocol):
    def create(
        self, spec: OutputSetSpec, operation: ScopedOperationIdentity
    ) -> OutputSetSnapshot: ...

    def stage(
        self,
        output: CaptureOutputVariant,
        intent_digest: str,
        custody: tuple[CustodyReceipt, ...],
    ) -> OutputReceipt: ...

    def find_stage_receipt(
        self, operation: ScopedOperationIdentity, intent_digest: str
    ) -> OutputReceipt | None: ...

    def seal(
        self, tenant_id: str, output_set_id: str, operation: ScopedOperationIdentity
    ) -> OutputSetSnapshot: ...

    def abort(
        self,
        tenant_id: str,
        output_set_id: str,
        operation: ScopedOperationIdentity,
        reason: str,
    ) -> OutputSetSnapshot: ...

    def reject(
        self,
        tenant_id: str,
        output_set_id: str,
        operation: ScopedOperationIdentity,
        reason: str,
    ) -> OutputSetSnapshot: ...

    def inspect(
        self, tenant_id: str, output_set_id: str
    ) -> OutputSetSnapshot | None: ...

    def read_published(
        self,
        tenant_id: str,
        output_set_id: str,
        *,
        after_output_id: str | None,
        limit: int,
    ) -> tuple[CaptureOutputVariant, ...]: ...


class OutputSetPublicationPort(Protocol):
    def publish(
        self,
        tenant_id: str,
        output_set_id: str,
        manifest_digest: str,
        operation: ScopedOperationIdentity,
    ) -> PublicationReceipt: ...


class PayloadContractValidationPort(Protocol):
    def validate(
        self, contract: PayloadContractRef, payload: Mapping[str, object]
    ) -> None: ...


class ArtifactCustodyPort(Protocol):
    def confirm(
        self, artifact: ArtifactRef, *, tenant_id: str, source: SourceRevisionRef
    ) -> CustodyReceipt: ...
