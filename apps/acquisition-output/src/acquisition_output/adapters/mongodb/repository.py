"""PyMongo implementation of the BC05-owned output ports.

All mutations use replica-set transactions. Output members remain one bounded
document each; the set document is a shared revision boundary for staging and
terminal transitions.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference
from pymongo.write_concern import WriteConcern

from acquisition_output.domain import (
    CaptureOutputVariant,
    CustodyReceipt,
    InvalidTransition,
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
    output_intent_digest,
    output_set_intent_digest,
    stream_manifest_digest,
)
from acquisition_output.ports import (
    OutputSetPublicationPort,
    OutputSetRepositoryPort,
    PendingOutboxEventPort,
)

from . import mapper
from .client import MongoResources

T = TypeVar("T")
_MAX_TX_ATTEMPTS = 4
_MAX_SEAL_RESCANS = 4


class _RevisionMoved(OutputError):
    pass


class MongoOutputStore(
    OutputSetRepositoryPort, OutputSetPublicationPort, PendingOutboxEventPort
):
    """BC05 persistence adapter. The supplied resource owner controls lifecycle."""

    def __init__(self, resources: MongoResources) -> None:
        if resources.client is None:
            raise RuntimeError("MongoResources must be entered before adapter creation")
        self._resources = resources
        self._client = resources.client
        self._db = resources.database
        self._sets = self._db["output_sets"]
        self._outputs = self._db["capture_outputs"]
        self._operations = self._db["output_operations"]
        self._outbox = self._db["output_outbox"]

    def _transaction(self, callback: Callable[[Any], T]) -> T:
        """Bounded retry using PyMongo's retryable transaction implementation."""
        last_error: BaseException | None = None
        for _ in range(_MAX_TX_ATTEMPTS):
            with self._client.start_session() as session:
                try:
                    return session.with_transaction(
                        callback,
                        read_concern=ReadConcern("snapshot"),
                        write_concern=WriteConcern("majority"),
                        read_preference=ReadPreference.PRIMARY,
                        max_commit_time_ms=self._resources.settings.transaction_commit_timeout_ms,
                    )
                except _RevisionMoved:
                    raise
                except PyMongoError as error:
                    if not error.has_error_label(
                        "TransientTransactionError"
                    ) and not error.has_error_label("UnknownTransactionCommitResult"):
                        raise
                    last_error = error
        raise OutputError("MongoDB transaction retry limit reached") from last_error

    @staticmethod
    def _operation_id(operation: ScopedOperationIdentity) -> str:
        return mapper.operation_storage_id(operation.tenant_id, operation.operation_id)

    def _replay(
        self, operation: ScopedOperationIdentity, intent: str, session: Any
    ) -> Any | None:
        document = self._operations.find_one(
            {"tenant_id": operation.tenant_id, "operation_id": operation.operation_id},
            session=session,
        )
        if document is None:
            return None
        if document["intent_digest"] != intent:
            raise OutputConflict("operation identity was reused with different intent")
        return self._operation_result(document)

    @staticmethod
    def _operation_result(document: dict[str, Any]) -> Any:
        kind = document["result_type"]
        result = document["result"]
        if kind == "output_receipt":
            return mapper.output_receipt_from_document(result)
        if kind == "publication_receipt":
            return mapper.publication_receipt_from_document(result)
        if kind == "snapshot":
            return mapper.snapshot_from_document(result)
        raise OutputConflict("stored operation receipt has an unknown result type")

    def _remember(
        self,
        operation: ScopedOperationIdentity,
        intent: str,
        kind: str,
        result: dict[str, Any],
        session: Any,
    ) -> None:
        document = {
            "_id": self._operation_id(operation),
            "tenant_id": operation.tenant_id,
            "operation_id": operation.operation_id,
            "intent_digest": intent,
            "result_type": kind,
            "result": result,
            "created_at_us": mapper.datetime_to_epoch_us(datetime.now(UTC)),
        }
        mapper.require_bson_budget(document)
        self._operations.insert_one(document, session=session)

    def create(
        self, spec: OutputSetSpec, operation: ScopedOperationIdentity
    ) -> OutputSetSnapshot:
        if operation.tenant_id != spec.binding.tenant_id:
            raise OutputError("create operation tenant differs from OutputSet tenant")
        intent = output_set_intent_digest(spec)
        now = datetime.now(UTC)

        def transaction(session: Any) -> OutputSetSnapshot:
            replay = self._replay(operation, intent, session)
            if replay is not None:
                if not isinstance(replay, OutputSetSnapshot):
                    raise OutputConflict(
                        "operation identity was used by another command"
                    )
                return replay
            key = {
                "tenant_id": spec.binding.tenant_id,
                "output_set_id": spec.output_set_id,
            }
            existing = self._sets.find_one(key, session=session)
            if existing is not None:
                root = mapper.root_from_document(existing)
                if output_set_intent_digest(root.spec) != intent:
                    raise OutputConflict(
                        "OutputSet identity already exists with different intent"
                    )
                snapshot = mapper.snapshot_from_document(existing)
            else:
                document = mapper.set_document(OutputSet(spec=spec, created_at=now))
                mapper.require_bson_budget(document)
                self._sets.insert_one(document, session=session)
                snapshot = mapper.snapshot_from_document(document)
            self._remember(
                operation,
                intent,
                "snapshot",
                mapper.set_document(
                    mapper.root_from_document(self._sets.find_one(key, session=session))
                ),
                session,
            )
            return snapshot

        try:
            return self._transaction(transaction)
        except DuplicateKeyError:
            replay = self._find_replay(operation, intent)
            if replay is not None:
                return replay
            raise OutputConflict("OutputSet or operation identity already exists")

    def stage(
        self,
        output: CaptureOutputVariant,
        intent_digest: str,
        custody: tuple[CustodyReceipt, ...],
    ) -> OutputReceipt:
        actual_intent = output_intent_digest(output)
        if intent_digest != actual_intent:
            raise OutputConflict("supplied intent digest differs from output contents")
        operation = output.operation
        set_key = {
            "tenant_id": output.binding.tenant_id,
            "output_set_id": output.output_set_id,
        }
        operation_intent = intent_digest
        document = mapper.output_document(output, intent_digest, custody)
        encoded_size = mapper.require_bson_budget(document)
        if encoded_size >= mapper.MAX_BSON_DOCUMENT_BYTES:
            raise OutputError(
                "CaptureOutput BSON must leave room below MongoDB's document limit"
            )
        receipt = OutputReceipt(
            operation.tenant_id,
            operation.operation_id,
            output.output_set_id,
            output.identity.output_id,
            intent_digest,
        )

        def transaction(session: Any) -> OutputReceipt:
            replay = self._replay(operation, operation_intent, session)
            if replay is not None:
                if not isinstance(replay, OutputReceipt):
                    raise OutputConflict(
                        "operation identity was used by another command"
                    )
                return replay
            raw = self._sets.find_one(set_key, session=session)
            if raw is None:
                raise OutputError("OutputSet does not exist")
            root = mapper.root_from_document(raw)
            root.validate_member(output)
            existing = self._outputs.find_one(
                {**set_key, "output_id": output.identity.output_id}, session=session
            )
            if existing is not None:
                if existing["intent_digest"] != intent_digest:
                    raise OutputConflict(
                        "output_id already exists with different intent"
                    )
                self._remember(
                    operation,
                    operation_intent,
                    "output_receipt",
                    mapper.output_receipt_document(receipt),
                    session,
                )
                return receipt
            if len(custody) != len(output.artifact_items):
                raise OutputError("each artifact reference requires confirmed custody")
            for item, confirmation in zip(output.artifact_items, custody, strict=True):
                if (
                    confirmation.tenant_id,
                    confirmation.artifact_id,
                    confirmation.digest,
                    confirmation.size_bytes,
                ) != (
                    output.binding.tenant_id,
                    item.artifact.artifact_id,
                    item.artifact.digest,
                    item.artifact.size_bytes,
                ):
                    raise OutputError("custody confirmation differs from ArtifactRef")
            updated = root.record_staged(output, custody_receipts=len(custody))
            self._outputs.insert_one(document, session=session)
            result = self._sets.update_one(
                {
                    "_id": raw["_id"],
                    "revision": raw["revision"],
                    "state": OutputSetState.STAGING.value,
                },
                {
                    "$set": {
                        "counters": {
                            "member_count": updated.member_count,
                            "encoded_output_bytes": updated.encoded_output_bytes,
                            "artifact_reference_count": updated.artifact_reference_count,
                            "custody_reference_count": updated.custody_reference_count,
                        }
                    },
                    "$inc": {"revision": 1},
                },
                session=session,
            )
            if result.modified_count != 1:
                raise _RevisionMoved("OutputSet stage boundary moved")
            self._remember(
                operation,
                operation_intent,
                "output_receipt",
                mapper.output_receipt_document(receipt),
                session,
            )
            return receipt

        try:
            return self._transaction(transaction)
        except DuplicateKeyError:
            replay = self._find_replay(operation, operation_intent)
            if replay is not None:
                return replay
            raise OutputConflict("output identity or operation identity already exists")

    def find_stage_receipt(
        self, operation: ScopedOperationIdentity, intent_digest: str
    ) -> OutputReceipt | None:
        document = self._operations.find_one(
            {"tenant_id": operation.tenant_id, "operation_id": operation.operation_id}
        )
        if document is None:
            return None
        if document["intent_digest"] != intent_digest:
            raise OutputConflict("operation identity was reused with different intent")
        result = self._operation_result(document)
        if not isinstance(result, OutputReceipt):
            raise OutputConflict(
                "operation identity was already used for another command"
            )
        return result

    def _manifest_digest(self, raw: dict[str, Any]) -> str:
        snapshot = mapper.snapshot_from_document(raw)
        cursor = self._outputs.find(
            {
                "tenant_id": snapshot.binding.tenant_id,
                "output_set_id": snapshot.output_set_id,
            },
            {
                "output_id": 1,
                "observation_id": 1,
                "operation_id": 1,
                "intent_digest": 1,
            },
            sort=[("output_id", ASCENDING), ("operation_id", ASCENDING)],
            batch_size=256,
        )

        def members():
            for row in cursor:
                yield MemberManifestEntry(
                    row["output_id"],
                    row["observation_id"],
                    row["operation_id"],
                    row["intent_digest"],
                )

        try:
            return stream_manifest_digest(snapshot, members())
        finally:
            cursor.close()

    def seal(
        self, tenant_id: str, output_set_id: str, operation: ScopedOperationIdentity
    ) -> OutputSetSnapshot:
        self._require_tenant(tenant_id, operation)
        intent = canonical_digest(
            {"command": "seal", "tenant_id": tenant_id, "output_set_id": output_set_id}
        )
        previous = self._find_replay(operation, intent)
        if previous is not None:
            if not isinstance(previous, OutputSetSnapshot):
                raise OutputConflict("operation identity was used by another command")
            return previous
        key = {"tenant_id": tenant_id, "output_set_id": output_set_id}
        for _ in range(_MAX_SEAL_RESCANS):
            raw = self._sets.find_one(key)
            if raw is None:
                raise OutputError("OutputSet does not exist")
            state = OutputSetState(raw["state"])
            if state == OutputSetState.SEALED:
                sealed_doc = raw

                def record_seal_replay(
                    session: Any, sealed_doc: dict[str, Any] = sealed_doc
                ) -> OutputSetSnapshot:
                    replay = self._replay(operation, intent, session)
                    if replay is not None:
                        if not isinstance(replay, OutputSetSnapshot):
                            raise OutputConflict(
                                "operation identity was used by another command"
                            )
                        return replay
                    self._remember(
                        operation,
                        intent,
                        "snapshot",
                        sealed_doc,
                        session,
                    )
                    return mapper.snapshot_from_document(sealed_doc)

                return self._transaction(record_seal_replay)
            if state != OutputSetState.STAGING:
                raise InvalidTransition("only a staging OutputSet can be sealed")
            try:
                digest = self._manifest_digest(raw)
            except OutputConflict:
                current = self._sets.find_one(key, {"revision": 1})
                if current is not None and current["revision"] != raw["revision"]:
                    continue
                raise
            root = mapper.root_from_document(raw)
            updated = root.seal(
                member_count=root.member_count,
                encoded_output_bytes=root.encoded_output_bytes,
                manifest_digest=digest,
                at=datetime.now(UTC),
            )
            set_doc = mapper.set_document(updated, revision=raw["revision"] + 1)

            def transaction(
                session: Any,
                raw: dict[str, Any] = raw,
                set_doc: dict[str, Any] = set_doc,
            ) -> OutputSetSnapshot:
                replay = self._replay(operation, intent, session)
                if replay is not None:
                    return replay
                result = self._sets.replace_one(
                    {
                        "_id": raw["_id"],
                        "revision": raw["revision"],
                        "state": "staging",
                    },
                    set_doc,
                    session=session,
                )
                if result.modified_count != 1:
                    raise _RevisionMoved(
                        "OutputSet changed during manifest verification"
                    )
                self._remember(operation, intent, "snapshot", set_doc, session)
                return mapper.snapshot_from_document(set_doc)

            try:
                return self._transaction(transaction)
            except _RevisionMoved:
                continue
            except DuplicateKeyError:
                replay = self._find_replay(operation, intent)
                if replay is not None:
                    return replay
        raise OutputError(
            "OutputSet changed during bounded manifest verification retries"
        )

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
        self._require_tenant(tenant_id, operation)
        command = "reject" if rejected else "abort"
        intent = canonical_digest(
            {
                "command": command,
                "tenant_id": tenant_id,
                "output_set_id": output_set_id,
                "reason": reason,
            }
        )
        key = {"tenant_id": tenant_id, "output_set_id": output_set_id}

        def transaction(session: Any) -> OutputSetSnapshot:
            replay = self._replay(operation, intent, session)
            if replay is not None:
                return replay
            raw = self._sets.find_one(key, session=session)
            if raw is None:
                raise OutputError("OutputSet does not exist")
            root = mapper.root_from_document(raw)
            updated = root.reject(reason) if rejected else root.abort(reason)
            doc = mapper.set_document(updated, revision=raw["revision"] + 1)
            result = self._sets.replace_one(
                {"_id": raw["_id"], "revision": raw["revision"]}, doc, session=session
            )
            if result.modified_count != 1:
                raise _RevisionMoved("OutputSet terminal transition conflicted")
            self._remember(operation, intent, "snapshot", doc, session)
            return mapper.snapshot_from_document(doc)

        try:
            return self._transaction(transaction)
        except DuplicateKeyError:
            replay = self._find_replay(operation, intent)
            if replay is not None:
                return replay
            raise OutputConflict("terminal operation identity already exists")

    def inspect(self, tenant_id: str, output_set_id: str) -> OutputSetSnapshot | None:
        raw = self._sets.find_one(
            {"tenant_id": tenant_id, "output_set_id": output_set_id}
        )
        return mapper.snapshot_from_document(raw) if raw is not None else None

    def read_published(
        self,
        tenant_id: str,
        output_set_id: str,
        *,
        after_output_id: str | None,
        limit: int,
    ) -> tuple[CaptureOutputVariant, ...]:
        if not 1 <= limit <= 1001:
            raise OutputError(
                "published output query limit is outside supported bounds"
            )
        gate = self._sets.find_one(
            {
                "tenant_id": tenant_id,
                "output_set_id": output_set_id,
                "state": OutputSetState.PUBLISHED.value,
            },
            {"_id": 1},
        )
        if gate is None:
            return ()
        query: dict[str, Any] = {"tenant_id": tenant_id, "output_set_id": output_set_id}
        if after_output_id is not None:
            query["output_id"] = {"$gt": after_output_id}
        cursor = self._outputs.find(
            query,
            sort=[("output_id", ASCENDING)],
            limit=limit,
            batch_size=min(limit, 256),
        )
        try:
            return tuple(mapper.output_from_document(row) for row in cursor)
        finally:
            cursor.close()

    def publish(
        self,
        tenant_id: str,
        output_set_id: str,
        manifest_digest: str,
        operation: ScopedOperationIdentity,
        authorization: PublicationAuthorization | None = None,
    ) -> PublicationReceipt:
        self._require_tenant(tenant_id, operation)
        auth_fields = None
        if authorization is not None:
            auth_fields = {
                "tenant_id": authorization.tenant_id,
                "run_id": authorization.run_id,
                "attempt_id": authorization.attempt_id,
                "fencing_generation": authorization.fencing_generation,
                "output_set_id": authorization.output_set_id,
                "manifest_digest": authorization.manifest_digest,
                "reservation_id": authorization.reservation_id,
                "operation_id": authorization.operation_id,
            }
        intent = canonical_digest(
            {
                "command": "publish",
                "tenant_id": tenant_id,
                "output_set_id": output_set_id,
                "manifest_digest": manifest_digest,
                "authorization": auth_fields,
            }
        )
        key = {"tenant_id": tenant_id, "output_set_id": output_set_id}
        publication_id = (
            "pub-" + canonical_digest([tenant_id, output_set_id, manifest_digest])[7:]
        )
        published_at = datetime.now(UTC)
        receipt = PublicationReceipt(
            tenant_id,
            operation.operation_id,
            output_set_id,
            manifest_digest,
            publication_id,
            published_at,
            authorization.reservation_id if authorization else None,
        )

        def transaction(session: Any) -> PublicationReceipt:
            replay = self._replay(operation, intent, session)
            if replay is not None:
                if not isinstance(replay, PublicationReceipt):
                    raise OutputConflict(
                        "operation identity was used by another command"
                    )
                return replay
            raw = self._sets.find_one(key, session=session)
            if raw is None:
                raise OutputError("OutputSet does not exist")
            root = mapper.root_from_document(raw)
            execution = root.binding.execution
            if execution.run_id is not None:
                if authorization is None or auth_fields is None:
                    raise OutputError(
                        "run-bound OutputSet requires a verified publication grant"
                    )
                if (
                    authorization.tenant_id != tenant_id
                    or authorization.run_id != execution.run_id
                    or authorization.attempt_id != execution.attempt_id
                    or authorization.fencing_generation != execution.fencing_token
                    or authorization.output_set_id != output_set_id
                    or authorization.manifest_digest != manifest_digest
                    or authorization.operation_id != operation.operation_id
                ):
                    raise OutputConflict(
                        "publication grant does not match OutputSet binding"
                    )
            elif authorization is not None:
                raise OutputConflict(
                    "unbound OutputSet cannot consume a publication grant"
                )
            if root.state == OutputSetState.PUBLISHED:
                if root.manifest_digest != manifest_digest:
                    raise OutputConflict("published OutputSet manifest cannot change")
                stored = raw.get("publication_receipt")
                if stored is None:
                    raise OutputConflict("published OutputSet is missing its receipt")
                existing_receipt = mapper.publication_receipt_from_document(stored)
                if existing_receipt.finalization_reservation_id != (
                    authorization.reservation_id if authorization else None
                ):
                    raise OutputConflict(
                        "published OutputSet has a different authorization"
                    )
                if (
                    authorization is not None
                    and existing_receipt.operation_id != authorization.operation_id
                ):
                    raise OutputConflict(
                        "published OutputSet has a different grant operation"
                    )
                self._remember(
                    operation,
                    intent,
                    "publication_receipt",
                    mapper.publication_receipt_document(existing_receipt),
                    session,
                )
                return existing_receipt
            updated = root.publish(manifest_digest=manifest_digest, at=published_at)
            doc = mapper.set_document(updated, revision=raw["revision"] + 1)
            doc["publication_receipt"] = mapper.publication_receipt_document(receipt)
            result = self._sets.replace_one(
                {"_id": raw["_id"], "revision": raw["revision"], "state": "sealed"},
                doc,
                session=session,
            )
            if result.modified_count != 1:
                raise _RevisionMoved("OutputSet publication conflicted")
            event_id = (
                "evt-" + canonical_digest(["OutputSetPublished/v1", publication_id])[7:]
            )
            event = {
                "_id": event_id,
                "event_id": event_id,
                "event_source": "urn:data-mozaic:bc05:acquisition-output",
                "event_type": "data-mozaic.acquisition.output-set-published.v1",
                "aggregate_type": "OutputSet",
                "aggregate_id": output_set_id,
                "tenant_id": tenant_id,
                "occurred_at_us": mapper.datetime_to_epoch_us(published_at),
                "correlation_id": operation.operation_id,
                "causation_id": operation.operation_id,
                "payload": {
                    "publication_id": publication_id,
                    "manifest_digest": manifest_digest,
                    "output_set_id": output_set_id,
                    "member_count": updated.member_count,
                },
                "state": "pending",
                "created_at_us": mapper.datetime_to_epoch_us(published_at),
            }
            mapper.require_bson_budget(event)
            self._outbox.insert_one(event, session=session)
            self._remember(
                operation,
                intent,
                "publication_receipt",
                mapper.publication_receipt_document(receipt),
                session,
            )
            return receipt

        try:
            return self._transaction(transaction)
        except DuplicateKeyError:
            replay = self._find_replay(operation, intent)
            if replay is not None:
                return replay
            raise OutputConflict(
                "publication identity or operation identity already exists"
            )

    def get_publication_receipt(
        self, tenant_id: str, output_set_id: str
    ) -> PublicationReceipt | None:
        raw = self._sets.find_one(
            {
                "tenant_id": tenant_id,
                "output_set_id": output_set_id,
                "state": OutputSetState.PUBLISHED.value,
            },
            {"publication_receipt": 1},
        )
        return (
            mapper.publication_receipt_from_document(raw["publication_receipt"])
            if raw and raw.get("publication_receipt")
            else None
        )

    def list_pending_events(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 1000:
            raise OutputError("outbox page size must be from 1 through 1000")
        cursor = self._outbox.find(
            {"state": "pending", "tenant_id": tenant_id},
            sort=[("created_at_us", ASCENDING), ("event_id", ASCENDING)],
            limit=limit,
        )
        try:
            return tuple(dict(row) for row in cursor)
        finally:
            cursor.close()

    def _find_replay(
        self, operation: ScopedOperationIdentity, intent: str
    ) -> Any | None:
        document = self._operations.find_one(
            {"tenant_id": operation.tenant_id, "operation_id": operation.operation_id}
        )
        if document is None:
            return None
        if document["intent_digest"] != intent:
            raise OutputConflict("operation identity was reused with different intent")
        return self._operation_result(document)

    @staticmethod
    def _require_tenant(tenant_id: str, operation: ScopedOperationIdentity) -> None:
        if operation.tenant_id != tenant_id:
            raise OutputError("operation tenant does not match requested tenant")
