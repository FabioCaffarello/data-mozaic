"""Acquisition import use cases that coordinate BC05 and BC06 public ports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import (
    ArtifactDescription,
    ArtifactItem,
    ArtifactOutput,
    ArtifactRef,
    CompositeOutput,
    OutputIdentity,
    OutputSetBinding,
    OutputSetSpec,
    PayloadContractRef,
    ProducerIdentity,
    ScopedOperationIdentity,
    SourceRevisionRef,
    derive_output_id,
)
from acquisition_output.domain import (
    ExecutionBinding as OutputExecutionBinding,
)
from mozaic_contract_resolution import (
    ContractReference,
    ContractResolutionError,
    ImmutableContractRegistry,
)

from .identities import stable_identifier as _stable_id
from .ports import (
    ArtifactCustodyPort,
    EvidenceReleaseAuthorizationPort,
    EvidenceReleasePort,
    ExecutionFinalizationPort,
)
from .result_bundle import (
    FixtureImportError,
    VerifiedResultBundle,
)


class ExactJSONSchemaPayloadContractValidator:
    def __init__(self, registry: ImmutableContractRegistry) -> None:
        self._registry = registry

    def validate(self, contract: PayloadContractRef, payload: dict[str, Any]) -> None:
        try:
            resolved = self._registry.resolve(
                ContractReference(
                    contract.contract_id, contract.version, contract.digest
                )
            )
            resolved.validate(payload)
        except ContractResolutionError as error:
            raise FixtureImportError(str(error)) from error


def _import_verified_bundle(
    application: AcquisitionOutputApplication,
    custody: ArtifactCustodyPort,
    *,
    bundle: VerifiedResultBundle,
    tenant_id: str,
    source: SourceRevisionRef,
    producer: ProducerIdentity,
    run_id: str,
    attempt_id: str,
    fencing_generation: int,
    observed_at: datetime,
    payload_contract: PayloadContractRef,
    artifact_representation: str = "synthetic_fixture_http_response_body",
    retention_policy_ref: str = "local-fixture-v1",
    artifact_title: str = "Synthetic fixture HTTP entity",
    artifact_role: str = "fixture-http-entity",
) -> tuple[str, str]:
    """Custody verified response entities, stage deterministic outputs, then seal."""
    references: dict[tuple[str, str, str], ArtifactRef] = {}
    for page in bundle.pages:
        artifact_id = _stable_id(
            "artifact-",
            {
                "tenant": tenant_id,
                "run": run_id,
                "attempt": attempt_id,
                "url": page.url,
                "page": page.evidence_ref,
                "digest": page.digest,
            },
        )
        operation_id = _stable_id(
            "op-",
            {
                "purpose": "fixture-custody",
                "tenant": tenant_id,
                "run": run_id,
                "attempt": attempt_id,
                "artifact": artifact_id,
            },
        )
        artifact = ArtifactRef(
            artifact_id, page.digest, len(page.body), page.media_type
        )
        custody.store_verified_entity(
            artifact=artifact,
            operation_id=operation_id,
            tenant_id=tenant_id,
            source=source,
            body=page.body,
            representation=artifact_representation,
            retention_policy_ref=retention_policy_ref,
            evidence_ref=page.evidence_ref,
        )
        references[
            (page.url, page.evidence_ref, page.digest.removeprefix("sha256:"))
        ] = artifact

    output_binding = OutputSetBinding(
        tenant_id,
        source,
        producer,
        OutputExecutionBinding(run_id, attempt_id, fencing_generation),
    )
    output_set_id = _stable_id(
        "set-",
        {
            "tenant": tenant_id,
            "run": run_id,
            "attempt": attempt_id,
            "records": bundle.records_digest,
        },
    )
    application.create_output_set(
        OutputSetSpec(
            output_set_id,
            output_binding,
            (payload_contract,),
            expected_output_count=len(bundle.records),
        ),
        ScopedOperationIdentity(
            tenant_id,
            _stable_id("op-", {"purpose": "create-set", "output_set": output_set_id}),
        ),
    )
    for record in bundle.records:
        provenance = record["_provenance"]
        page_key = (
            provenance["source_url"],
            provenance["evidence_ref"],
            provenance["raw_sha256"],
        )
        artifact = references.get(page_key)
        if artifact is None:
            raise FixtureImportError("record has no custody-confirmed fixture page")
        observation_id = _stable_id(
            "obs-",
            {
                "run": run_id,
                "attempt": attempt_id,
                "page_index": provenance["page_index"],
                "record_index": provenance["record_index"],
                "records_digest": bundle.records_digest,
            },
        )
        output_id = derive_output_id(
            tenant_id=tenant_id,
            source=source,
            producer=producer,
            output_key=f"fixture-record:{provenance['page_index']}:{provenance['record_index']}",
            observation_id=observation_id,
        )
        output = CompositeOutput(
            identity=OutputIdentity(
                output_id,
                observation_id,
                next(
                    (
                        page.acquired_at
                        for page in bundle.pages
                        if page.url == provenance["source_url"]
                        and page.evidence_ref == provenance["evidence_ref"]
                        and page.digest.removeprefix("sha256:")
                        == provenance["raw_sha256"]
                        and page.acquired_at is not None
                    ),
                    observed_at,
                ),
                natural_key=record.get("id"),
            ),
            operation=ScopedOperationIdentity(
                tenant_id,
                _stable_id("op-", {"purpose": "stage-output", "output_id": output_id}),
            ),
            output_set_id=output_set_id,
            binding=output_binding,
            payload_contract=payload_contract,
            payload=record,
            artifacts=(
                ArtifactItem(
                    artifact,
                    ArtifactDescription(
                        title=artifact_title,
                        role=artifact_role,
                    ),
                ),
            ),
        )
        application.stage_output(output)
    sealed = application.seal_output_set(
        tenant_id,
        output_set_id,
        ScopedOperationIdentity(
            tenant_id,
            _stable_id("op-", {"purpose": "seal-set", "output_set": output_set_id}),
        ),
    )
    return output_set_id, sealed.manifest_digest


class TrustedAcquisitionApplication:
    """Application-level result staging over the existing BC05/BC06 APIs.

    BC04 context is read from its repository port for each import. Callers may
    provide a result directory and a verified compiled plan, but cannot supply
    the run, attempt, generation, source or release identity used for outputs.
    """

    def __init__(
        self,
        execution_contexts,
        outputs: AcquisitionOutputApplication,
        custody: ArtifactCustodyPort,
        finalization: ExecutionFinalizationPort | None = None,
        evidence_release: EvidenceReleasePort | None = None,
        evidence_release_authorizer: EvidenceReleaseAuthorizationPort | None = None,
    ):
        self._execution_contexts = execution_contexts
        self._outputs = outputs
        self._custody = custody
        self._finalization = finalization
        self._evidence_release = evidence_release
        self._evidence_release_authorizer = evidence_release_authorizer

    def _evidence_release_plan(
        self, tenant_id: str, run_id: str, bundle: VerifiedResultBundle
    ):
        """Resolve owner facts required before any exact Gateway release."""
        from .context import AcquisitionApplicationError

        snapshot = self._execution_contexts.get(tenant_id, run_id)
        reservation = None if snapshot is None else snapshot.reservation
        if (
            snapshot is None
            or snapshot.intent.tenant_id != tenant_id
            or snapshot.intent.run_id != run_id
            or snapshot.state.value != "succeeded"
            or snapshot.attempt_state is None
            or snapshot.attempt_state.value != "succeeded"
            or reservation is None
            or snapshot.publication_receipt is None
            or reservation.tenant_id != tenant_id
            or reservation.run_id != run_id
            or reservation.attempt_id != snapshot.attempt_id
            or reservation.fencing_generation != snapshot.generation
        ):
            raise AcquisitionApplicationError(
                "BC04 completion or finalization is unresolved"
            )
        context = self._verification_context(tenant_id, run_id)
        if (
            context.attempt_id != reservation.attempt_id
            or context.generation != reservation.fencing_generation
        ):
            raise AcquisitionApplicationError(
                "BC04 current attempt differs from reservation"
            )
        output_set_id = reservation.output_set_id
        output_set = self._outputs.inspect_output_set(tenant_id, output_set_id)
        receipt = self._outputs.get_publication_receipt(tenant_id, output_set_id)
        bc04_receipt = snapshot.publication_receipt
        if (
            output_set is None
            or output_set.state.value != "published"
            or output_set.binding.tenant_id != tenant_id
            or output_set.binding.execution.run_id != run_id
            or output_set.binding.execution.attempt_id != reservation.attempt_id
            or output_set.binding.execution.fencing_token
            != reservation.fencing_generation
            or output_set.manifest_digest != reservation.manifest_digest
            or receipt is None
            or receipt.tenant_id != tenant_id
            or receipt.output_set_id != output_set_id
            or receipt.manifest_digest != reservation.manifest_digest
            or receipt.finalization_reservation_id != reservation.reservation_id
            or bc04_receipt.get("tenant_id") != tenant_id
            or bc04_receipt.get("output_set_id") != output_set_id
            or bc04_receipt.get("manifest_digest") != reservation.manifest_digest
            or bc04_receipt.get("finalization_reservation_id")
            != reservation.reservation_id
            or bc04_receipt.get("publication_id") != receipt.publication_id
        ):
            raise AcquisitionApplicationError(
                "BC04 and BC05 publication facts are inconsistent"
            )

        published_refs: dict[str, ArtifactRef] = {}
        cursor = None
        while True:
            page = self._outputs.read_published_outputs(
                tenant_id, output_set_id, cursor=cursor, limit=100
            )
            for output in page.outputs:
                if (
                    output.output_set_id != output_set_id
                    or output.binding.tenant_id != tenant_id
                    or output.binding.execution.run_id != run_id
                    or output.binding.execution.attempt_id != reservation.attempt_id
                    or output.binding.execution.fencing_token
                    != reservation.fencing_generation
                ):
                    raise AcquisitionApplicationError(
                        "published membership escaped exact execution binding"
                    )
                for item in output.artifact_items:
                    prior = published_refs.get(item.artifact.artifact_id)
                    if prior is not None and prior != item.artifact:
                        raise AcquisitionApplicationError(
                            "published artifact identity conflicts"
                        )
                    published_refs[item.artifact.artifact_id] = item.artifact
            cursor = page.next_cursor
            if cursor is None:
                break

        expected: list[tuple[str, str, int, ArtifactRef]] = []
        source = SourceRevisionRef(context.source_id, context.source_revision_id)
        for fetched in bundle.pages:
            identity = (
                {
                    "tenant": tenant_id,
                    "run": run_id,
                    "attempt": context.attempt_id,
                    "fetch": fetched.evidence_ref,
                    "digest": fetched.digest,
                }
                if context.output_modality == "artifact"
                else {
                    "tenant": tenant_id,
                    "run": run_id,
                    "attempt": context.attempt_id,
                    "url": fetched.url,
                    "page": fetched.evidence_ref,
                    "digest": fetched.digest,
                }
            )
            artifact_id = _stable_id("artifact-", identity)
            artifact = published_refs.get(artifact_id)
            if (
                artifact is None
                or artifact.digest != fetched.digest
                or artifact.size_bytes != fetched.acquired_size
            ):
                raise AcquisitionApplicationError(
                    "published OutputSet does not bind every exact FetchEvidence"
                )
            if not self._custody.verify_existing_artifact(
                tenant_id=tenant_id, artifact=artifact, source=source
            ):
                raise AcquisitionApplicationError(
                    "BC06 has not confirmed exact durable custody"
                )
            expected.append(
                (fetched.evidence_ref, fetched.digest, fetched.acquired_size, artifact)
            )

        # BC04 completion and BC05 publication are durable immutable facts, but
        # re-read the execution owner immediately before authorizing release.
        current = self._execution_contexts.get(tenant_id, run_id)
        if (
            current is None
            or current.state.value != "succeeded"
            or current.attempt_state is None
            or current.attempt_state.value != "succeeded"
            or current.generation != snapshot.generation
            or current.reservation != reservation
            or current.publication_receipt != bc04_receipt
        ):
            raise AcquisitionApplicationError(
                "BC04 release eligibility changed during inspection"
            )
        return context, reservation, tuple(expected)

    def inspect_evidence_release(
        self, tenant_id: str, run_id: str, bundle: VerifiedResultBundle
    ) -> dict[str, object]:
        """Read-only eligibility inspection using BC04, BC05 and BC06 owners."""
        context, reservation, evidence = self._evidence_release_plan(
            tenant_id, run_id, bundle
        )
        return {
            "eligible": True,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "attempt_id": reservation.attempt_id,
            "generation": reservation.fencing_generation,
            "output_set_id": reservation.output_set_id,
            "manifest_digest": reservation.manifest_digest,
            "fetch_ids": [item[0] for item in evidence],
            "evidence_count": len(evidence),
            "profile": context.output_modality,
        }

    def release_published_evidence(
        self,
        tenant_id: str,
        run_id: str,
        bundle: VerifiedResultBundle,
        *,
        operator_identity: str,
    ) -> dict[str, object]:
        """Explicit trusted operator release after owner-verified durable publication."""
        from .context import AcquisitionApplicationError

        if not operator_identity.strip():
            raise AcquisitionApplicationError("trusted operator identity is required")
        if self._evidence_release_authorizer is None:
            raise AcquisitionApplicationError(
                "evidence release operator authorization is not configured"
            )
        self._evidence_release_authorizer.assert_can_release(
            operator_identity, tenant_id, run_id
        )
        if self._evidence_release is None:
            raise AcquisitionApplicationError(
                "Gateway evidence release is not configured"
            )
        context, reservation, evidence = self._evidence_release_plan(
            tenant_id, run_id, bundle
        )
        results = []
        for fetch_id, digest, size, _artifact in evidence:
            result = self._evidence_release.release(
                tenant_id=tenant_id,
                run_id=run_id,
                attempt_id=reservation.attempt_id,
                generation=reservation.fencing_generation,
                fetch_id=fetch_id,
                digest=digest,
            )
            results.append(
                {
                    "fetch_id": fetch_id,
                    "size_bytes": size,
                    "body_state": result["body_state"],
                }
            )
        return {
            "eligible": True,
            "operator_identity": operator_identity,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "attempt_id": reservation.attempt_id,
            "generation": reservation.fencing_generation,
            "output_set_id": reservation.output_set_id,
            "profile": context.output_modality,
            "released": results,
        }

    def _context(
        self, tenant_id: str, run_id: str, *, allow_published_replay: bool = False
    ):
        from .context import load_current_execution

        return load_current_execution(
            self._execution_contexts,
            tenant_id,
            run_id,
            allow_published_replay=allow_published_replay,
        )

    def _verification_context(self, tenant_id: str, run_id: str):
        from .context import AcquisitionApplicationError

        try:
            return self._context(tenant_id, run_id, allow_published_replay=True)
        except AcquisitionApplicationError as error:
            raise FixtureImportError(str(error)) from error

    def finalize_output_set(self, lease, output_set_id: str, manifest_digest: str):
        """Request BC04's existing reservation/grant/publication protocol."""
        from .context import AcquisitionApplicationError

        if self._finalization is None:
            raise AcquisitionApplicationError(
                "BC04 finalization port is not configured"
            )
        context = self._context(lease.tenant_id, lease.run_id)
        if (
            lease.tenant_id != context.tenant_id
            or lease.run_id != context.run_id
            or lease.attempt_id != context.attempt_id
            or lease.generation != context.generation
        ):
            raise AcquisitionApplicationError(
                "finalization lease differs from current BC04 attempt"
            )
        return self._finalization.reserve_and_publish(
            lease,
            output_set_id,
            manifest_digest,
            "publish-" + output_set_id,
        )

    def reconcile_finalization(self, tenant_id: str, run_id: str):
        """Delegate receipt lookup and completion to the BC04 owner."""
        from .context import AcquisitionApplicationError

        if self._finalization is None:
            raise AcquisitionApplicationError(
                "BC04 finalization port is not configured"
            )
        snapshot = self._execution_contexts.get(tenant_id, run_id)
        if (
            snapshot is None
            or snapshot.intent.tenant_id != tenant_id
            or snapshot.intent.run_id != run_id
            or snapshot.reservation is None
        ):
            raise AcquisitionApplicationError(
                "BC04 has no matching pending reservation"
            )
        return self._finalization.reconcile_finalization(tenant_id, run_id)

    def verify_fixture_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        result_dir: Path,
        execution_report,
        fixture_root: Path,
        catalog_entry,
        build_root: Path,
        execution_report_schema_path: Path,
    ) -> VerifiedResultBundle:
        from .compiled_plan import load_compiled_plan
        from .result_bundle import verify_result_bundle

        context = self._verification_context(tenant_id, run_id)
        binding = execution_report.binding
        if (
            context.execution_mode != "fixture-only"
            or execution_report.run_id != context.run_id
            or execution_report.attempt_id != context.attempt_id
            or execution_report.request_id != context.request_id
            or hashlib.sha256(
                json.dumps(
                    asdict(binding), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            != context.binding_digest
            or binding.source_id != context.source_id
            or binding.source_revision != context.source_revision_id
            or binding.bot_id != context.bot_id
            or binding.build_id != context.build_id
            or "sha256:" + binding.bot_build_manifest_digest
            != context.build_manifest_digest
        ):
            raise FixtureImportError("fixture result differs from current BC04 attempt")
        compiled_plan, bot_id = load_compiled_plan(build_root, context)
        return verify_result_bundle(
            result_dir,
            execution_report,
            fixture_root=fixture_root,
            catalog_entry=catalog_entry,
            compiled_plan=compiled_plan,
            execution_report_schema_path=execution_report_schema_path,
            trusted_bot_id=bot_id,
        )

    def verify_live_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        result_dir: Path,
        gateway_evidence_url: str,
        gateway_evidence_token: str,
        build_root: Path,
        max_response_bytes: int,
    ) -> VerifiedResultBundle:
        from .compiled_plan import load_compiled_plan
        from .live_evidence import verify_live_result_bundle

        context = self._verification_context(tenant_id, run_id)
        if (
            context.execution_mode != "live-authorized"
            or context.policy_digest is None
            or max_response_bytes < 1
            or context.maximum_total_response_bytes < 1
        ):
            raise FixtureImportError("live import has no trusted BC04 policy binding")
        compiled_plan, bot_id = load_compiled_plan(build_root, context)
        return verify_live_result_bundle(
            result_dir,
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            generation=context.generation,
            source_id=context.source_id,
            source_revision_id=context.source_revision_id,
            policy_digest=context.policy_digest,
            release_id=context.release_id,
            build_manifest_digest=context.build_manifest_digest,
            gateway_evidence_url=gateway_evidence_url,
            gateway_evidence_token=gateway_evidence_token,
            compiled_plan=compiled_plan,
            trusted_bot_id=bot_id,
            max_response_bytes=min(
                max_response_bytes, context.maximum_total_response_bytes
            ),
            maximum_total_response_bytes=context.maximum_total_response_bytes,
            maximum_requests=context.maximum_requests,
            execution_deadline=context.deadline,
            input_parameters_json=context.input_parameters_json,
            input_parameters_digest=context.input_parameters_digest,
            output_modality=context.output_modality,
            artifact_media_types=context.artifact_media_types,
            max_artifact_bytes=context.max_artifact_bytes,
        )

    def import_fixture_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        result_dir: Path,
        execution_report,
        fixture_root: Path,
        catalog_entry,
        build_root: Path,
        execution_report_schema_path: Path,
        payload_contract: PayloadContractRef,
        producer: ProducerIdentity,
        artifact_representation: str = "synthetic_fixture_http_response_body",
        retention_policy_ref: str = "local-fixture-v1",
        artifact_title: str = "Synthetic fixture HTTP entity",
        artifact_role: str = "fixture-http-entity",
    ) -> tuple[str, str, VerifiedResultBundle]:
        bundle = self.verify_fixture_result(
            tenant_id,
            run_id,
            result_dir=result_dir,
            execution_report=execution_report,
            fixture_root=fixture_root,
            catalog_entry=catalog_entry,
            build_root=build_root,
            execution_report_schema_path=execution_report_schema_path,
        )
        result = self.import_verified_result(
            tenant_id,
            run_id,
            bundle=bundle,
            payload_contract=payload_contract,
            producer=producer,
            artifact_representation=artifact_representation,
            retention_policy_ref=retention_policy_ref,
            artifact_title=artifact_title,
            artifact_role=artifact_role,
        )
        return result[0], result[1], bundle

    def import_live_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        result_dir: Path,
        gateway_evidence_url: str,
        gateway_evidence_token: str,
        build_root: Path,
        max_response_bytes: int,
        payload_contract: PayloadContractRef,
        producer: ProducerIdentity,
        artifact_representation: str = "synthetic_fixture_http_response_body",
        retention_policy_ref: str = "local-fixture-v1",
        artifact_title: str = "Synthetic fixture HTTP entity",
        artifact_role: str = "fixture-http-entity",
    ) -> tuple[str, str, VerifiedResultBundle]:
        bundle = self.verify_live_result(
            tenant_id,
            run_id,
            result_dir=result_dir,
            gateway_evidence_url=gateway_evidence_url,
            gateway_evidence_token=gateway_evidence_token,
            build_root=build_root,
            max_response_bytes=max_response_bytes,
        )
        result = self.import_verified_result(
            tenant_id,
            run_id,
            bundle=bundle,
            payload_contract=payload_contract,
            producer=producer,
            artifact_representation=artifact_representation,
            retention_policy_ref=retention_policy_ref,
            artifact_title=artifact_title,
            artifact_role=artifact_role,
        )
        return result[0], result[1], bundle

    def import_live_artifact_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        result_dir: Path,
        gateway_evidence_url: str,
        gateway_evidence_token: str,
        build_root: Path,
        max_response_bytes: int,
        producer: ProducerIdentity,
        retention_policy_ref: str,
    ) -> tuple[str, str, VerifiedResultBundle]:
        """Verify the raw-only worker bundle, then custody and stage its bytes."""
        bundle = self.verify_live_result(
            tenant_id,
            run_id,
            result_dir=result_dir,
            gateway_evidence_url=gateway_evidence_url,
            gateway_evidence_token=gateway_evidence_token,
            build_root=build_root,
            max_response_bytes=max_response_bytes,
        )
        try:
            result = self.import_verified_artifact_result(
                tenant_id,
                run_id,
                bundle=bundle,
                producer=producer,
                retention_policy_ref=retention_policy_ref,
            )
            return result[0], result[1], bundle
        finally:
            bundle.cleanup()

    def import_verified_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        bundle: VerifiedResultBundle,
        payload_contract: PayloadContractRef,
        producer: ProducerIdentity,
        observed_at: datetime | None = None,
        artifact_representation: str = "synthetic_fixture_http_response_body",
        retention_policy_ref: str = "local-fixture-v1",
        artifact_title: str = "Synthetic fixture HTTP entity",
        artifact_role: str = "fixture-http-entity",
    ) -> tuple[str, str]:
        context = self._verification_context(tenant_id, run_id)
        if (
            context.payload_contract is None
            or payload_contract != context.payload_contract
        ):
            raise FixtureImportError(
                "payload contract differs from the trusted execution binding"
            )
        if (
            producer.producer_id != bundle.trusted_bot_id
            or (context.bot_id is not None and producer.producer_id != context.bot_id)
            or producer.release_id != context.release_id
        ):
            raise FixtureImportError("producer differs from trusted BotBuild binding")
        if producer.release_digest != context.build_manifest_digest:
            raise FixtureImportError("producer digest differs from trusted BotBuild")
        if any(
            record.get("_provenance", {}).get("source_id") != context.source_id
            or record.get("_provenance", {}).get("source_revision")
            != context.source_revision_id
            for record in bundle.records
        ):
            raise FixtureImportError(
                "record provenance differs from BC04 source binding"
            )
        if context.replay_output_set_id is not None:
            output_set_id = _stable_id(
                "set-",
                {
                    "tenant": context.tenant_id,
                    "run": context.run_id,
                    "attempt": context.attempt_id,
                    "records": bundle.records_digest,
                },
            )
            snapshot = self._outputs.inspect_output_set(
                context.tenant_id, output_set_id
            )
            expected_execution = OutputExecutionBinding(
                context.run_id, context.attempt_id, context.generation
            )
            if (
                output_set_id != context.replay_output_set_id
                or context.replay_manifest_digest is None
                or snapshot is None
                or snapshot.state.value != "published"
                or snapshot.manifest_digest != context.replay_manifest_digest
                or snapshot.binding.tenant_id != context.tenant_id
                or snapshot.binding.source.source_id != context.source_id
                or snapshot.binding.source.revision != context.source_revision_id
                or snapshot.binding.producer != producer
                or snapshot.binding.execution != expected_execution
                or snapshot.member_count != len(bundle.records)
                or payload_contract not in snapshot.payload_contracts
            ):
                raise FixtureImportError(
                    "completed replay differs from BC04 reservation and published BC05 set"
                )
            return output_set_id, snapshot.manifest_digest
        return _import_verified_bundle(
            self._outputs,
            self._custody,
            bundle=bundle,
            tenant_id=context.tenant_id,
            source=SourceRevisionRef(context.source_id, context.source_revision_id),
            producer=producer,
            run_id=context.run_id,
            attempt_id=context.attempt_id,
            fencing_generation=context.generation,
            observed_at=observed_at or context.created_at,
            payload_contract=payload_contract,
            artifact_representation=artifact_representation,
            retention_policy_ref=retention_policy_ref,
            artifact_title=artifact_title,
            artifact_role=artifact_role,
        )

    def import_verified_artifact_result(
        self,
        tenant_id: str,
        run_id: str,
        *,
        bundle: VerifiedResultBundle,
        producer: ProducerIdentity,
        retention_policy_ref: str,
    ) -> tuple[str, str]:
        """Custody and stage one evidence-verified raw artifact without records."""
        context = self._verification_context(tenant_id, run_id)
        if (
            context.output_modality != "artifact"
            or not bundle.artifact_only
            or bundle.records
            or len(bundle.pages) != 1
            or context.max_artifact_bytes is None
        ):
            raise FixtureImportError(
                "artifact result differs from the BC04 output binding"
            )
        page = bundle.pages[0]
        if (
            page.body_representation != "decoded-http-entity"
            or page.media_type not in context.artifact_media_types
            or page.acquired_size > context.max_artifact_bytes
            or (
                page.body_path is None
                and page.digest != "sha256:" + hashlib.sha256(page.body).hexdigest()
            )
        ):
            raise FixtureImportError(
                "artifact bytes differ from the trusted output constraints"
            )
        if (
            producer.producer_id != bundle.trusted_bot_id
            or (context.bot_id is not None and producer.producer_id != context.bot_id)
            or producer.release_id != context.release_id
            or producer.release_digest != context.build_manifest_digest
        ):
            raise FixtureImportError(
                "artifact producer differs from the trusted BotBuild binding"
            )
        output_set_id = _stable_id(
            "set-",
            {
                "tenant": tenant_id,
                "run": run_id,
                "attempt": context.attempt_id,
                "fetch": page.evidence_ref,
            },
        )
        if context.replay_output_set_id is not None:
            snapshot = self._outputs.inspect_output_set(tenant_id, output_set_id)
            if (
                context.replay_output_set_id != output_set_id
                or context.replay_manifest_digest is None
                or snapshot is None
                or snapshot.state.value != "published"
                or snapshot.manifest_digest != context.replay_manifest_digest
                or snapshot.member_count != 1
                or snapshot.artifact_reference_count != 1
            ):
                raise FixtureImportError(
                    "completed artifact replay differs from its published OutputSet"
                )
            return output_set_id, snapshot.manifest_digest
        source = SourceRevisionRef(context.source_id, context.source_revision_id)
        artifact_id = _stable_id(
            "artifact-",
            {
                "tenant": tenant_id,
                "run": run_id,
                "attempt": context.attempt_id,
                "fetch": page.evidence_ref,
                "digest": page.digest,
            },
        )
        artifact = ArtifactRef(
            artifact_id, page.digest, page.acquired_size, page.media_type
        )
        self._custody.store_verified_entity(
            artifact=artifact,
            operation_id=_stable_id(
                "op-", {"purpose": "raw-artifact-custody", "artifact": artifact_id}
            ),
            tenant_id=tenant_id,
            source=source,
            body=page.body_path if page.body_path is not None else page.body,
            representation=page.body_representation,
            retention_policy_ref=retention_policy_ref,
            evidence_ref=page.evidence_ref,
        )
        binding = OutputSetBinding(
            tenant_id,
            source,
            producer,
            OutputExecutionBinding(run_id, context.attempt_id, context.generation),
        )
        self._outputs.create_output_set(
            OutputSetSpec(output_set_id, binding, expected_output_count=1),
            ScopedOperationIdentity(
                tenant_id,
                _stable_id("op-", {"purpose": "create-set", "set": output_set_id}),
            ),
        )
        observation_id = _stable_id(
            "obs-",
            {"run": run_id, "attempt": context.attempt_id, "fetch": page.evidence_ref},
        )
        output_id = derive_output_id(
            tenant_id=tenant_id,
            source=source,
            producer=producer,
            output_key="raw-artifact:" + page.evidence_ref,
            observation_id=observation_id,
        )
        self._outputs.stage_output(
            ArtifactOutput(
                identity=OutputIdentity(
                    output_id, observation_id, page.acquired_at or context.created_at
                ),
                operation=ScopedOperationIdentity(
                    tenant_id,
                    _stable_id(
                        "op-", {"purpose": "stage-artifact", "output": output_id}
                    ),
                ),
                output_set_id=output_set_id,
                binding=binding,
                artifacts=(
                    ArtifactItem(
                        artifact,
                        ArtifactDescription(
                            title=f"Acquired {page.media_type} artifact",
                            role="raw-acquired-artifact",
                        ),
                    ),
                ),
            )
        )
        sealed = self._outputs.seal_output_set(
            tenant_id,
            output_set_id,
            ScopedOperationIdentity(
                tenant_id,
                _stable_id("op-", {"purpose": "seal-set", "set": output_set_id}),
            ),
        )
        return output_set_id, sealed.manifest_digest
