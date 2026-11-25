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
from jsonschema import Draft202012Validator, FormatChecker

from .identities import stable_identifier as _stable_id
from .ports import ArtifactCustodyPort, ExecutionFinalizationPort
from .result_bundle import (
    FixtureImportError,
    VerifiedResultBundle,
    _read_regular,
    _strict_json,
)


class ExactJSONSchemaPayloadContractValidator:
    def __init__(self, contract: PayloadContractRef, schema_path: Path) -> None:
        schema_data = _read_regular(schema_path, maximum=64_000, label="payload schema")
        if "sha256:" + hashlib.sha256(schema_data).hexdigest() != contract.digest:
            raise FixtureImportError("payload contract bytes differ from its digest")
        schema = _strict_json(schema_data, "payload schema")
        Draft202012Validator.check_schema(schema)
        self._contract = contract
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def validate(self, contract: PayloadContractRef, payload: dict[str, Any]) -> None:
        if contract != self._contract:
            raise FixtureImportError(
                "payload contract differs from the resolved contract"
            )
        self._validator.validate(payload)


def import_verified_bundle(
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
    ):
        self._execution_contexts = execution_contexts
        self._outputs = outputs
        self._custody = custody
        self._finalization = finalization

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
        return import_verified_bundle(
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
