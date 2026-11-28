"""Import one protected live worker result using BC04 and lifecycle bindings."""

from __future__ import annotations

import json
from pathlib import Path

from acquisition_output.domain import PayloadContractRef, ProducerIdentity


class TrustedLiveAttemptImporter:
    def __init__(
        self,
        *,
        acquisition,
        lifecycle,
        lifecycle_store,
        gateway_url: str,
        gateway_import_token: str,
    ) -> None:
        self._acquisition = acquisition
        self._lifecycle = lifecycle
        self._lifecycle_store = lifecycle_store
        self._gateway_url = gateway_url
        self._gateway_import_token = gateway_import_token

    def import_attempt(self, *, submission, run, lease, result):
        if not isinstance(result, Path) or result.is_symlink() or not result.is_dir():
            raise ValueError("protected worker result directory is invalid")
        binding = run.intent.binding
        release = self._lifecycle.resolve_eligible_release(binding.release_id)
        approved = release["approved_release"]
        if (
            approved["source_id"] != binding.source_id
            or approved["source_revision_id"] != binding.source_revision_id
            or approved["build_id"] != binding.build_id
            or approved["build_manifest_digest"] != binding.build_manifest_digest
            or approved["runtime_image_digest"] != binding.runtime_image_digest
            or approved["release_id"] != binding.release_id
        ):
            raise ValueError("approved release differs from the BC04 run binding")
        build = self._lifecycle_store.resolve_build(
            binding.build_id, binding.runtime_image_digest
        )
        build_root = Path(build["artifact_path"])
        manifest_path = build_root / "manifest.json"
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > 1_000_000
        ):
            raise ValueError("registered BotBuild manifest is unavailable or unsafe")
        manifest = json.loads(manifest_path.read_bytes())
        if (
            manifest.get("build_id") != binding.build_id
            or manifest.get("bot_id") != approved["bot_id"]
            or manifest.get("backend_id") != "scrapy-live-http"
        ):
            raise ValueError(
                "registered BotBuild manifest differs from the approved release"
            )
        producer = ProducerIdentity(
            producer_id=manifest["bot_id"],
            producer_version="collector-live-v1",
            capability_id=manifest["backend_id"],
            capability_version=manifest["backend_version"],
            release_id=approved["release_id"],
            release_digest=binding.build_manifest_digest,
        )
        if binding.output_modality == "artifact":
            policy = self._lifecycle_store.resolve_policy(
                binding.policy_id, binding.policy_version
            )
            retention = policy.get("evidence_retention_policy_ref")
            if not isinstance(retention, str) or not retention:
                raise ValueError("approved policy has no artifact retention reference")
            output_set_id, manifest_digest, _bundle = (
                self._acquisition.import_live_artifact_result(
                    submission.intent.tenant_id,
                    run.intent.run_id,
                    result_dir=result,
                    gateway_evidence_url=self._gateway_url,
                    gateway_evidence_token=self._gateway_import_token,
                    build_root=build_root,
                    max_response_bytes=binding.max_total_response_bytes,
                    producer=producer,
                    retention_policy_ref=retention,
                )
            )
            try:
                self._acquisition.finalize_output_set(
                    lease, output_set_id, manifest_digest
                )
            finally:
                _bundle.cleanup()
            return output_set_id, manifest_digest
        contract = PayloadContractRef(
            binding.payload_contract_id,
            binding.payload_contract_version,
            binding.payload_contract_digest,
        )
        output_set_id, manifest_digest, _bundle = self._acquisition.import_live_result(
            submission.intent.tenant_id,
            run.intent.run_id,
            result_dir=result,
            gateway_evidence_url=self._gateway_url,
            gateway_evidence_token=self._gateway_import_token,
            build_root=build_root,
            max_response_bytes=binding.max_total_response_bytes,
            payload_contract=contract,
            producer=producer,
        )
        try:
            self._acquisition.finalize_output_set(lease, output_set_id, manifest_digest)
        finally:
            _bundle.cleanup()
        return output_set_id, manifest_digest
