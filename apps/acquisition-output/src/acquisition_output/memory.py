"""Bounded reference repository; process-local, not durable or distributed."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from .domain import (
    CaptureOutputVariant,
    CustodyReceipt,
    MemberManifestEntry,
    OutputConflict,
    OutputError,
    OutputReceipt,
    OutputSet,
    OutputSetSnapshot,
    OutputSetSpec,
    OutputSetState,
    PublicationAuthorization,
    PublicationReceipt,
    ScopedOperationIdentity,
    canonical_digest,
    canonical_json,
    output_intent_digest,
    output_set_intent_digest,
    stream_manifest_digest,
)


@dataclass(frozen=True, slots=True)
class _StoredMember:
    output: CaptureOutputVariant
    intent_digest: str
    custody_receipts: tuple[CustodyReceipt, ...]
    encoded_bytes: int


class InMemoryOutputStore:
    """Implements the BC05 repository and publication ports under one lock.

    Atomicity is limited to this Python process. State is lost at process exit;
    the implementation does not provide cross-process uniqueness, a durable
    outbox, recovery, PostgreSQL fencing, or distributed finalization.
    """

    def __init__(
        self, *, max_sets: int = 100, max_stored_bytes: int = 268_435_456
    ) -> None:
        if isinstance(max_sets, bool) or not isinstance(max_sets, int) or max_sets < 1:
            raise ValueError("max_sets must be positive")
        if (
            isinstance(max_stored_bytes, bool)
            or not isinstance(max_stored_bytes, int)
            or max_stored_bytes < 1
        ):
            raise ValueError("max_stored_bytes must be positive")
        self._lock = threading.RLock()
        self._max_sets = max_sets
        self._max_stored_bytes = max_stored_bytes
        self._stored_bytes = 0
        self._sets: dict[tuple[str, str], OutputSet] = {}
        self._members: dict[tuple[str, str], dict[str, _StoredMember]] = {}
        self._operations: dict[tuple[str, str], tuple[str, object]] = {}
        self._publications: dict[tuple[str, str], PublicationReceipt] = {}

    def create(
        self, spec: OutputSetSpec, operation: ScopedOperationIdentity
    ) -> OutputSetSnapshot:
        if operation.tenant_id != spec.binding.tenant_id:
            raise OutputError("create operation tenant differs from OutputSet tenant")
        intent = output_set_intent_digest(spec)
        key = (operation.tenant_id, operation.operation_id)
        set_key = (spec.binding.tenant_id, spec.output_set_id)
        with self._lock:
            replay = self._replay(key, intent)
            if replay is not None:
                return replay  # type: ignore[return-value]
            existing = self._sets.get(set_key)
            if existing is not None:
                if output_set_intent_digest(existing.spec) != intent:
                    raise OutputConflict(
                        "OutputSet identity already exists with different intent"
                    )
                result = self._snapshot(existing)
            else:
                if len(self._sets) >= self._max_sets:
                    raise OutputError("in-memory reference store set limit reached")
                root = OutputSet(spec=spec)
                self._sets[set_key] = root
                self._members[set_key] = {}
                result = self._snapshot(root)
            self._remember(key, intent, result)
            return result

    def stage(
        self,
        output: CaptureOutputVariant,
        intent_digest: str,
        custody: tuple[CustodyReceipt, ...],
    ) -> OutputReceipt:
        actual_intent = output_intent_digest(output)
        if intent_digest != actual_intent:
            raise OutputConflict(
                "supplied output intent digest does not match its contents"
            )
        operation = output.operation
        operation_key = (operation.tenant_id, operation.operation_id)
        set_key = (output.binding.tenant_id, output.output_set_id)
        with self._lock:
            replay = self._replay(operation_key, intent_digest)
            if replay is not None:
                return replay  # type: ignore[return-value]
            root = self._sets.get(set_key)
            if root is None:
                raise OutputError("OutputSet does not exist")
            root.validate_member(output)
            members = self._members[set_key]
            if output.identity.output_id in members:
                old = members[output.identity.output_id]
                if old.intent_digest == intent_digest:
                    receipt = OutputReceipt(
                        operation.tenant_id,
                        operation.operation_id,
                        output.output_set_id,
                        output.identity.output_id,
                        intent_digest,
                    )
                    self._remember(operation_key, intent_digest, receipt)
                    return receipt
                raise OutputConflict("output_id already exists with different intent")
            self._validate_custody(output, custody)
            updated = root.record_staged(output, custody_receipts=len(custody))
            encoded_bytes = len(canonical_json(output.to_wire()))
            if self._stored_bytes + encoded_bytes > self._max_stored_bytes:
                raise OutputError("in-memory reference store byte limit reached")
            members[output.identity.output_id] = _StoredMember(
                output, intent_digest, custody, encoded_bytes
            )
            self._sets[set_key] = updated
            self._stored_bytes += encoded_bytes
            receipt = OutputReceipt(
                operation.tenant_id,
                operation.operation_id,
                output.output_set_id,
                output.identity.output_id,
                intent_digest,
            )
            self._remember(operation_key, intent_digest, receipt)
            return receipt

    def seal(
        self, tenant_id: str, output_set_id: str, operation: ScopedOperationIdentity
    ) -> OutputSetSnapshot:
        self._require_operation_tenant(tenant_id, operation)
        intent = canonical_digest(
            {"command": "seal", "tenant_id": tenant_id, "output_set_id": output_set_id}
        )
        op_key = (tenant_id, operation.operation_id)
        set_key = (tenant_id, output_set_id)
        with self._lock:
            replay = self._replay(op_key, intent)
            if replay is not None:
                return replay  # type: ignore[return-value]
            root = self._required_set(set_key)
            members = self._members[set_key]
            candidate = self._snapshot(root)
            digest = stream_manifest_digest(candidate, self._manifest_members(set_key))
            updated = root.seal(
                member_count=len(members),
                encoded_output_bytes=sum(
                    member.encoded_bytes for member in members.values()
                ),
                manifest_digest=digest,
                at=datetime.now(UTC),
            )
            self._sets[set_key] = updated
            result = self._snapshot(updated)
            self._remember(op_key, intent, result)
            return result

    def find_stage_receipt(
        self, operation: ScopedOperationIdentity, intent_digest: str
    ) -> OutputReceipt | None:
        key = (operation.tenant_id, operation.operation_id)
        with self._lock:
            result = self._replay(key, intent_digest)
            if result is None:
                return None
            if not isinstance(result, OutputReceipt):
                raise OutputConflict(
                    "operation identity was already used for another command"
                )
            return result

    def publish(
        self,
        tenant_id: str,
        output_set_id: str,
        expected_manifest_digest: str,
        operation: ScopedOperationIdentity,
        authorization: PublicationAuthorization | None = None,
    ) -> PublicationReceipt:
        self._require_operation_tenant(tenant_id, operation)
        authorization_intent = (
            {
                field: getattr(authorization, field)
                for field in (
                    "tenant_id",
                    "run_id",
                    "attempt_id",
                    "fencing_generation",
                    "output_set_id",
                    "manifest_digest",
                    "reservation_id",
                    "operation_id",
                )
            }
            if authorization is not None
            else None
        )
        intent = canonical_digest(
            {
                "command": "publish",
                "tenant_id": tenant_id,
                "output_set_id": output_set_id,
                "manifest_digest": expected_manifest_digest,
                "authorization": authorization_intent,
            }
        )
        op_key = (tenant_id, operation.operation_id)
        set_key = (tenant_id, output_set_id)
        with self._lock:
            replay = self._replay(op_key, intent)
            if replay is not None:
                return replay  # type: ignore[return-value]
            root = self._required_set(set_key)
            execution = root.binding.execution
            if execution.run_id is not None:
                if authorization is None or any(
                    getattr(authorization, field, None) != expected
                    for field, expected in (
                        ("tenant_id", tenant_id),
                        ("run_id", execution.run_id),
                        ("attempt_id", execution.attempt_id),
                        ("fencing_generation", execution.fencing_token),
                        ("output_set_id", output_set_id),
                        ("manifest_digest", expected_manifest_digest),
                        ("operation_id", operation.operation_id),
                    )
                ):
                    raise OutputError(
                        "run-bound OutputSet requires a matching publication grant"
                    )
            elif authorization is not None:
                raise OutputConflict(
                    "unbound OutputSet cannot consume a publication grant"
                )
            if root.manifest_digest != expected_manifest_digest:
                raise OutputConflict(
                    "publication digest does not match the sealed membership"
                )
            updated = root.publish(
                manifest_digest=expected_manifest_digest, at=datetime.now(UTC)
            )
            receipt = self._publications.get(set_key)
            if receipt is None:
                receipt = PublicationReceipt(
                    tenant_id=tenant_id,
                    operation_id=operation.operation_id,
                    output_set_id=output_set_id,
                    manifest_digest=expected_manifest_digest,
                    publication_id=str(uuid4()),
                    published_at=updated.published_at or datetime.now(UTC),
                    finalization_reservation_id=(
                        getattr(authorization, "reservation_id", None)
                    ),
                )
            elif (
                receipt.manifest_digest != expected_manifest_digest
                or receipt.finalization_reservation_id
                != getattr(authorization, "reservation_id", None)
            ):
                raise OutputConflict("published OutputSet cannot change manifest")
            self._sets[set_key] = updated
            self._publications[set_key] = receipt
            self._remember(op_key, intent, receipt)
            return receipt

    def abort(
        self,
        tenant_id: str,
        output_set_id: str,
        operation: ScopedOperationIdentity,
        reason: str,
    ) -> OutputSetSnapshot:
        return self._terminate(
            tenant_id, output_set_id, operation, reason, rejected=False
        )

    def reject(
        self,
        tenant_id: str,
        output_set_id: str,
        operation: ScopedOperationIdentity,
        reason: str,
    ) -> OutputSetSnapshot:
        return self._terminate(
            tenant_id, output_set_id, operation, reason, rejected=True
        )

    def _terminate(
        self,
        tenant_id: str,
        output_set_id: str,
        operation: ScopedOperationIdentity,
        reason: str,
        *,
        rejected: bool,
    ) -> OutputSetSnapshot:
        self._require_operation_tenant(tenant_id, operation)
        action = "reject" if rejected else "abort"
        intent = canonical_digest(
            {
                "command": action,
                "tenant_id": tenant_id,
                "output_set_id": output_set_id,
                "reason": reason,
            }
        )
        op_key = (tenant_id, operation.operation_id)
        set_key = (tenant_id, output_set_id)
        with self._lock:
            replay = self._replay(op_key, intent)
            if replay is not None:
                return replay  # type: ignore[return-value]
            root = self._required_set(set_key)
            updated = root.reject(reason) if rejected else root.abort(reason)
            self._sets[set_key] = updated
            result = self._snapshot(updated)
            self._remember(op_key, intent, result)
            return result

    def inspect(self, tenant_id: str, output_set_id: str) -> OutputSetSnapshot | None:
        with self._lock:
            root = self._sets.get((tenant_id, output_set_id))
            return self._snapshot(root) if root else None

    def get_publication_receipt(
        self, tenant_id: str, output_set_id: str
    ) -> PublicationReceipt | None:
        with self._lock:
            return self._publications.get((tenant_id, output_set_id))

    def list_pending_events(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[object, ...]:
        if not 1 <= limit <= 1000:
            raise OutputError("pending outbox page size must be from 1 through 1000")
        return ()

    def read_published(
        self,
        tenant_id: str,
        output_set_id: str,
        *,
        after_output_id: str | None,
        limit: int,
    ) -> tuple[CaptureOutputVariant, ...]:
        with self._lock:
            key = (tenant_id, output_set_id)
            root = self._sets.get(key)
            if root is None or root.state != OutputSetState.PUBLISHED:
                return ()
            rows = self._members[key]
            ids = [
                item
                for item in sorted(rows)
                if after_output_id is None or item > after_output_id
            ]
            return tuple(rows[output_id].output for output_id in ids[:limit])

    @staticmethod
    def _validate_custody(
        output: CaptureOutputVariant, receipts: tuple[CustodyReceipt, ...]
    ) -> None:
        expected = {
            (item.artifact.artifact_id, item.artifact.digest, item.artifact.size_bytes)
            for item in output.artifact_items
        }
        actual = {
            (item.artifact_id, item.digest, item.size_bytes)
            for item in receipts
            if item.tenant_id == output.binding.tenant_id
        }
        if len(receipts) != len(expected) or actual != expected:
            raise OutputError(
                "artifact output requires one matching custody receipt per reference"
            )

    def _manifest_members(self, key: tuple[str, str]):
        members = self._members.get(key, {})
        for member in sorted(
            members.values(),
            key=lambda row: (
                row.output.identity.output_id,
                row.output.operation.operation_id,
            ),
        ):
            yield MemberManifestEntry(
                output_id=member.output.identity.output_id,
                observation_id=member.output.identity.observation_id,
                operation_id=member.output.operation.operation_id,
                intent_digest=member.intent_digest,
            )

    def _snapshot(self, root: OutputSet) -> OutputSetSnapshot:
        value = OutputSetSnapshot(
            output_set_id=root.output_set_id,
            binding=root.binding,
            state=root.state,
            payload_contracts=root.spec.payload_contracts,
            member_count=root.member_count,
            encoded_output_bytes=root.encoded_output_bytes,
            artifact_reference_count=root.artifact_reference_count,
            custody_reference_count=root.custody_reference_count,
            manifest_digest=root.manifest_digest,
            created_at=root.created_at,
            sealed_at=root.sealed_at,
            published_at=root.published_at,
        )
        return value

    def _required_set(self, key: tuple[str, str]) -> OutputSet:
        root = self._sets.get(key)
        if root is None:
            raise OutputError("OutputSet does not exist")
        return root

    @staticmethod
    def _require_operation_tenant(
        tenant_id: str, operation: ScopedOperationIdentity
    ) -> None:
        if operation.tenant_id != tenant_id:
            raise OutputError("operation tenant differs from target tenant")

    def _replay(self, key: tuple[str, str], intent: str) -> object | None:
        previous = self._operations.get(key)
        if previous is None:
            return None
        previous_intent, result = previous
        if previous_intent != intent:
            raise OutputConflict(
                "operation identity was reused with different canonical intent"
            )
        return result

    def _remember(self, key: tuple[str, str], intent: str, result: object) -> None:
        self._operations[key] = (intent, result)
