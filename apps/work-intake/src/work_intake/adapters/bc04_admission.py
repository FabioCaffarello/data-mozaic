"""BC13-to-BC04 admission adapter using the public durable coordinator API."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from execution_coordination.durable_application import DurableExecutionCoordinator

from work_intake.domain import DispatchRejected, Submission


class BC04LiveRunAdmission:
    """Create one deterministic BC04 Run for one immutable Submission intent."""

    def __init__(self, coordinator: DurableExecutionCoordinator) -> None:
        self._coordinator = coordinator

    def admit(self, submission: Submission, release: Mapping[str, Any]) -> str:
        intent = submission.intent
        if (
            release.get("release_id") != intent.release_id
            or release.get("contract_version") != "bot-release/v3"
            or release.get("source_id") != intent.source_id
            or release.get("source_revision_id") != intent.source_revision_id
            or release.get("input_contract")
            != {
                "contract_id": intent.input_contract.contract_id,
                "version": intent.input_contract.version,
                "digest": intent.input_contract.digest,
            }
        ):
            raise DispatchRejected("release_binding_mismatch")
        parameters = json.loads(intent.parameters_json.decode("utf-8", errors="strict"))
        snapshot = self._coordinator.admit_live_run(
            tenant_id=intent.tenant_id,
            run_id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "data-mozaic:bc04:" + intent.dispatch_operation_id,
                )
            ),
            request_id=intent.submission_id,
            idempotency_key=intent.dispatch_operation_id,
            deadline=intent.created_at + timedelta(minutes=5),
            source_id=intent.source_id,
            source_revision_id=intent.source_revision_id,
            policy_id=str(release["policy_id"]),
            policy_version=str(release["policy_version"]),
            release_id=intent.release_id,
            build_id=str(release["build_id"]),
            build_manifest_digest=str(release["build_manifest_digest"]),
            runtime_image_digest=str(release["runtime_image_digest"]),
            input_id=intent.input_id,
            submission_id=intent.submission_id,
            input_parameters=parameters,
            input_contract={
                "contract_id": intent.input_contract.contract_id,
                "version": intent.input_contract.version,
                "digest": intent.input_contract.digest,
            },
        )
        return snapshot.intent.run_id

    def resolve_existing(self, submission: Submission) -> str | None:
        """Recover an acknowledged BC04 commit before rechecking release eligibility."""
        intent = submission.intent
        snapshot = self._coordinator.resolve_admission(
            intent.tenant_id, intent.dispatch_operation_id
        )
        if snapshot is None:
            return None
        expected_run_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "data-mozaic:bc04:" + intent.dispatch_operation_id,
            )
        )
        binding = snapshot.intent.binding
        expected_contract = intent.input_contract
        expected_parameters = intent.parameters_json.decode("utf-8", errors="strict")
        expected_deadline = intent.created_at + timedelta(minutes=5)
        if (
            snapshot.intent.run_id != expected_run_id
            or snapshot.intent.request_id != intent.submission_id
            or snapshot.intent.idempotency_key != intent.dispatch_operation_id
            or snapshot.intent.tenant_id != intent.tenant_id
            or snapshot.intent.deadline != expected_deadline
            or snapshot.intent.max_attempts != 3
            or getattr(binding, "execution_mode", None) != "live-authorized"
            or getattr(binding, "source_id", None) != intent.source_id
            or getattr(binding, "source_revision_id", None) != intent.source_revision_id
            or getattr(binding, "release_id", None) != intent.release_id
            or getattr(binding, "input_id", None) != intent.input_id
            or getattr(binding, "submission_id", None) != intent.submission_id
            or getattr(binding, "input_parameters_json", None) != expected_parameters
            or getattr(binding, "input_parameters_digest", None)
            != intent.parameters_digest
            or getattr(binding, "input_contract_id", None)
            != expected_contract.contract_id
            or getattr(binding, "input_contract_version", None)
            != expected_contract.version
            or getattr(binding, "input_contract_digest", None)
            != expected_contract.digest
        ):
            raise DispatchRejected("admission_intent_conflict")
        return snapshot.intent.run_id
