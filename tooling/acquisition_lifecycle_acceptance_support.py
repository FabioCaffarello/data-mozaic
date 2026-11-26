"""Test-only operator composition for the controlled M4 lifecycle vertical."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from acquisition_lifecycle.adapters.bc04_authority import BC04AuthoritySnapshotAdapter
from acquisition_lifecycle.adapters.postgres import PostgresLifecycleStore
from acquisition_lifecycle.adapters.signing import Ed25519AuthoritySigner
from acquisition_lifecycle.application import AcquisitionLifecycle
from acquisition_lifecycle.domain import canonical_digest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from execution_coordination.domain import CoordinationError
from mozaic_contract_resolution import ContractReference, ImmutableContractRegistry

_ACTIVE_APP: AcquisitionLifecycle | None = None
_ACTIVE_RELEASE_ID: str | None = None


def active_lifecycle() -> AcquisitionLifecycle:
    """Return the lifecycle application installed by the current acceptance."""
    if _ACTIVE_APP is None:
        raise RuntimeError("controlled lifecycle acceptance is not configured")
    return _ACTIVE_APP


class ControlledTestApproval:
    """Approval authority whose identity and scope are limited to this harness."""

    def assert_can_approve(self, actor_id: str, release_id: str) -> str:
        if actor_id != "controlled-test-operator" or not release_id.startswith(
            "controlled-live-release-"
        ):
            raise ValueError("controlled approval authority rejected the request")
        return f"test-operator://{actor_id}/release/{release_id}"


def publish_controlled_release(
    *,
    root: Path,
    source_id: str,
    revision_id: str,
    source_path_prefixes: tuple[str, ...],
    policy: dict[str, Any],
    payload_contract: ContractReference | None,
    build_manifest: dict[str, Any],
    build_directory: Path,
    playbook_path: Path,
    runtime_digest: str,
    input_contract: ContractReference | None = None,
    output_contract: dict[str, Any] | None = None,
    release_id: str,
    expected_generation: int,
    authority_id: str,
) -> tuple[bytes, dict[str, bytes]]:
    """Use the production lifecycle app with explicitly test-scoped trust inputs."""
    durable = PostgresDurableRunRepository(_required_env("BC04_DATABASE_URL"))
    store = PostgresLifecycleStore(
        _required_env("BC04_DATABASE_URL"), root / "migrations"
    )
    artifact_root = root / ".local/acquisition-lifecycle/builds"
    artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    registered_artifact = artifact_root / build_manifest["build_id"]
    if not registered_artifact.exists():
        if build_directory.is_symlink() or not build_directory.is_dir():
            raise RuntimeError(
                "controlled BotBuild directory is not a regular directory"
            )
        shutil.copytree(build_directory, registered_artifact, symlinks=True)
    if registered_artifact.is_symlink() or not registered_artifact.is_dir():
        raise RuntimeError("registered controlled BotBuild path is unsafe")

    key_id = "controlled-m4-test-authority"
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    application = AcquisitionLifecycle(
        store,
        ImmutableContractRegistry(root / "contracts/payload/draft/v1"),
        BC04AuthoritySnapshotAdapter(durable),
        Ed25519AuthoritySigner(key_id, private_key),
        {key_id: public_key},
        ControlledTestApproval(),
        authority_id=authority_id,
    )
    global _ACTIVE_APP, _ACTIVE_RELEASE_ID
    _ACTIVE_APP = application
    _ACTIVE_RELEASE_ID = release_id

    source: dict[str, Any] = {
        "contract_version": "source-revision/v1",
        "source_id": source_id,
        "revision_id": revision_id,
        "owner": "Data Mozaic controlled test infrastructure",
        "status": "eligible",
        "scope": {
            "origins": ["https://api.fixture.test"],
            "methods": ["GET"],
            "path_prefixes": list(source_path_prefixes),
            "query_parameter_values": dict(policy.get("query_parameter_values", {})),
        },
        "configuration_version": "1.0.0",
        "configuration_digest": "",
        "metadata": {"classification": "controlled-test-only"},
    }
    source["configuration_digest"] = canonical_digest(
        {key: value for key, value in source.items() if key != "configuration_digest"}
    )
    source_ref = application.register_source_revision(
        source, actor_id="controlled-test-operator"
    )
    policy_ref = application.register_policy_revision(
        policy, actor_id="controlled-test-operator"
    )
    playbook_digest = application.register_playbook_revision(
        playbook_path, actor_id="controlled-test-operator"
    )
    registered_build = application.register_verified_build(
        registered_artifact,
        runtime_image_digest=runtime_digest,
        actor_id="controlled-test-operator",
    )
    if registered_build["manifest"]["build_id"] != build_manifest["build_id"]:
        raise RuntimeError("registered build identity differs from worker image build")

    if payload_contract is not None:
        ImmutableContractRegistry(root / "contracts/payload/draft/v1").resolve(
            payload_contract
        )
    application.create_release_candidate(
        release_id=release_id,
        source=source_ref,
        policy=policy_ref,
        build_id=registered_build["build_id"],
        runtime_image_digest=registered_build["runtime_image_digest"],
        contract=payload_contract,
        input_contract=input_contract,
        output_contract=output_contract,
        playbook_digest=playbook_digest,
        actor_id="controlled-test-operator",
    )
    application.approve_release(release_id, actor_id="controlled-test-operator")
    envelope, _authority = application.publish_authority_snapshot()
    snapshot = json.loads(envelope)["snapshot"]
    if snapshot["generation"] != expected_generation:
        raise RuntimeError(
            "BC04 authority generation changed during controlled lifecycle"
        )
    return envelope, {key_id: public_key}


def revoke_and_verify_admission_denial() -> dict[str, Any]:
    if _ACTIVE_APP is None or _ACTIVE_RELEASE_ID is None:
        raise RuntimeError("controlled lifecycle state was not initialized")
    app = _ACTIVE_APP
    release_id = _ACTIVE_RELEASE_ID
    before = app.inspect_release(release_id)
    previous = before.get("approved_release")
    if not isinstance(previous, dict):
        raise TypeError("controlled release history has no approved document")
    app.revoke_release(release_id, actor_id="controlled-test-operator")
    envelope, authority = app.publish_authority_snapshot()
    historical = app.inspect_release(release_id)
    if historical["state"] != "revoked" or historical["approved_release"] != previous:
        raise RuntimeError("revocation changed immutable historical release content")
    try:
        authority.resolve(
            source_id=previous["source_id"],
            source_revision_id=previous["source_revision_id"],
            policy_id=previous["policy_id"],
            policy_version=previous["policy_version"],
            release_id=release_id,
            build_id=previous["build_id"],
            build_manifest_digest=previous["build_manifest_digest"],
            runtime_image_digest=previous["runtime_image_digest"],
        )
    except CoordinationError:
        pass
    else:
        raise RuntimeError("revoked BotRelease remained admissible in BC04 authority")
    return {
        "release_id": release_id,
        "state": historical["state"],
        "authority_generation": json.loads(envelope)["snapshot"]["generation"],
        "historical_release_preserved": historical["approved_release"] == previous,
        "new_admission_rejected": True,
    }


def _required_env(name: str) -> str:
    import os

    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is required for the controlled lifecycle acceptance"
        )
    return value
