"""Use cases that compose BC01, BC08, BC11, BC15 and BC04 public capabilities."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import rfc8785
from collector_engineering.live_admission import validate_live_admission
from collector_engineering.playbook_v2 import (
    parse_playbook,
    validate_playbook_v2,
)
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from mozaic_bot_build import canonical_json, verify_bot_build
from mozaic_contract_resolution import ContractReference, ImmutableContractRegistry

from .domain import (
    LifecycleError,
    PolicyRevisionRef,
    SourceRevisionRef,
    canonical_digest,
    immutable_document_digest,
    require_identifier,
)
from .ports import (
    AuthoritySignerPort,
    AuthoritySnapshotPort,
    LifecycleStore,
    OperatorApprovalPort,
)

_ROOT = Path(__file__).resolve().parents[4]
_SOURCE_SCHEMA = _ROOT / "contracts/source-revision/draft/v1/SourceRevision.schema.json"
_POLICY_SCHEMA = (
    _ROOT / "contracts/acquisition-policy/draft/v1/AcquisitionPolicy.schema.json"
)
_RELEASE_SCHEMA = _ROOT / "contracts/bot-release/draft/v2/BotRelease.schema.json"
_RELEASE_SCHEMA_V3 = _ROOT / "contracts/bot-release/draft/v3/BotRelease.schema.json"
_RELEASE_SCHEMA_V4 = _ROOT / "contracts/bot-release/draft/v4/BotRelease.schema.json"
_RELEASE_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")


class AcquisitionLifecycle:
    """Small operator-facing composition; provider state stays owner-local."""

    def __init__(
        self,
        store: LifecycleStore,
        contracts: ImmutableContractRegistry,
        authority_registry: AuthoritySnapshotPort,
        signer: AuthoritySignerPort,
        trusted_public_keys: dict[str, bytes],
        approval: OperatorApprovalPort,
        *,
        authority_id: str,
    ):
        self._store = store
        self._contracts = contracts
        self._authority_registry = authority_registry
        self._signer = signer
        self._trusted_public_keys = dict(trusted_public_keys)
        self._approval = approval
        self._authority_id = require_identifier(authority_id, "authority identity")

    def register_source_revision(
        self, document: dict[str, Any], *, actor_id: str
    ) -> SourceRevisionRef:
        source = _strict_schema_document(document, _SOURCE_SCHEMA, "SourceRevision")
        if source.get("status") != "eligible":
            raise LifecycleError("new SourceRevision must begin eligible")
        if source.get("configuration_digest") != immutable_document_digest(
            source, "configuration_digest"
        ):
            raise LifecycleError("SourceRevision configuration digest is invalid")
        digest = self._store.register_source(source, _actor(actor_id))
        return SourceRevisionRef(source["source_id"], source["revision_id"], digest)

    def register_policy_revision(
        self, document: dict[str, Any], *, actor_id: str
    ) -> PolicyRevisionRef:
        policy = _strict_schema_document(document, _POLICY_SCHEMA, "AcquisitionPolicy")
        if policy.get("status") != "eligible":
            raise LifecycleError("new AcquisitionPolicy must begin eligible")
        if policy.get("policy_digest") != immutable_document_digest(
            policy, "policy_digest"
        ):
            raise LifecycleError("AcquisitionPolicy digest is invalid")
        source = self._store.resolve_source(
            policy["source_id"], policy["source_revision_id"]
        )
        if source["status"] != "eligible":
            raise LifecycleError(
                "AcquisitionPolicy cannot bind an ineligible source revision"
            )
        if not set(policy["origins"]).issubset(source["scope"]["origins"]) or not set(
            policy["methods"]
        ).issubset(source["scope"]["methods"]):
            raise LifecycleError(
                "AcquisitionPolicy exceeds its SourceRevision origin or method scope"
            )
        if any(
            not any(
                _path_is_within(path, prefix)
                for prefix in source["scope"]["path_prefixes"]
            )
            for path in policy["path_prefixes"]
        ):
            raise LifecycleError(
                "AcquisitionPolicy exceeds its SourceRevision path scope"
            )
        source_queries = source["scope"]["query_parameter_values"]
        if any(
            key not in source_queries or not set(values).issubset(source_queries[key])
            for key, values in policy["query_parameter_values"].items()
        ):
            raise LifecycleError(
                "AcquisitionPolicy exceeds its SourceRevision query scope"
            )
        digest = self._store.register_policy(policy, _actor(actor_id))
        return PolicyRevisionRef(policy["policy_id"], policy["policy_version"], digest)

    def revoke_source_revision(
        self, source_id: str, revision_id: str, *, actor_id: str
    ) -> None:
        self._store.set_source_state(
            source_id, revision_id, "revoked", _actor(actor_id)
        )

    def revoke_policy_revision(
        self, policy_id: str, version: str, *, actor_id: str
    ) -> None:
        self._store.set_policy_state(policy_id, version, "revoked", _actor(actor_id))

    def register_playbook_revision(self, path: Path, *, actor_id: str) -> str:
        if path.is_symlink() or not path.is_file():
            raise LifecycleError("Playbook source must be a regular file")
        content = path.read_bytes()
        if not content or len(content) > 1_000_000:
            raise LifecycleError("Playbook revision exceeds the supported size")
        if path.is_symlink() or path.read_bytes() != content:
            raise LifecycleError("CapturePlaybook changed while it was being validated")
        return self._register_playbook_content(content, actor_id=actor_id)

    def register_playbook_document(
        self, document: dict[str, Any], *, actor_id: str
    ) -> dict[str, str]:
        """Register an already parsed declarative candidate through BC15 validation.

        Used by trusted application compositions such as Scout proposal
        promotion. This operation registers immutable Playbook content only;
        release eligibility and approval remain separate M4 operations.
        """
        if not isinstance(document, dict):
            raise LifecycleError("CapturePlaybook candidate must be an object")
        content = (
            json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")
        digest = self._register_playbook_content(content, actor_id=actor_id)
        return {"playbook_id": document["playbook_id"], "playbook_digest": digest}

    def _register_playbook_content(self, content: bytes, *, actor_id: str) -> str:
        try:
            parsed = parse_playbook(content.decode("utf-8"))
        except Exception as error:
            raise LifecycleError("CapturePlaybook revision is invalid") from error
        validation = validate_playbook_v2(parsed)
        if not validation.valid:
            raise LifecycleError("CapturePlaybook revision failed BC15 validation")
        document = parsed.document
        source = self._store.resolve_source(
            document["source"]["source_id"], document["source"]["revision"]
        )
        if source["status"] != "eligible":
            raise LifecycleError("Playbook revision source is not eligible")
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        self._store.register_playbook(
            document["playbook_id"],
            digest,
            document["source"]["source_id"],
            document["source"]["revision"],
            content,
            _actor(actor_id),
        )
        return digest

    def register_verified_build(
        self,
        artifact_path: Path,
        *,
        runtime_image_digest: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image_digest):
            raise LifecycleError("worker runtime image digest is invalid")
        if artifact_path.is_symlink() or not artifact_path.is_dir():
            raise LifecycleError("BotBuild artifact path is not a regular directory")
        manifest_path = artifact_path / "manifest.json"
        if (
            manifest_path.is_symlink()
            or not manifest_path.is_file()
            or manifest_path.stat().st_size > 1_000_000
        ):
            raise LifecycleError("BotBuild manifest path is unsafe or oversized")
        manifest_bytes = manifest_path.read_bytes()
        raw_digest = hashlib.sha256(manifest_bytes).hexdigest()
        try:
            verified_build = verify_bot_build(artifact_path, raw_digest)
        except Exception as error:
            raise LifecycleError("BotBuild verification failed") from error
        manifest = verified_build.manifest
        if (
            manifest.get("compatibility", {}).get("execution_mode") != "live-authorized"
            or manifest.get("compatibility", {}).get("runtime_profile")
            != "scrapy-live-http-v1"
            or manifest.get("backend_id") != "scrapy-live-http"
        ):
            raise LifecycleError(
                "BotBuild is incompatible with authorized Scrapy live execution"
            )
        manifest_digest = (
            "sha256:" + hashlib.sha256(canonical_json(manifest)).hexdigest()
        )
        self._store.register_build(
            manifest["build_id"],
            manifest_digest,
            runtime_image_digest,
            artifact_path,
            manifest,
            _actor(actor_id),
        )
        return {
            "build_id": manifest["build_id"],
            "manifest_digest": manifest_digest,
            "runtime_image_digest": runtime_image_digest,
            "manifest": manifest,
            "artifact_path": artifact_path.resolve(strict=True),
        }

    def create_release_candidate(
        self,
        *,
        release_id: str,
        source: SourceRevisionRef,
        policy: PolicyRevisionRef,
        build_id: str,
        runtime_image_digest: str,
        contract: ContractReference | None,
        input_contract: ContractReference | None = None,
        output_contract: dict[str, Any] | None = None,
        playbook_digest: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if not _RELEASE_ID.fullmatch(release_id):
            raise LifecycleError("BotRelease identity is invalid")
        actor_id = _actor(actor_id)
        source_doc = self._store.resolve_source(source.source_id, source.revision_id)
        if (
            source_doc["status"] != "eligible"
            or source_doc["_revision_digest"] != source.digest
        ):
            raise LifecycleError(
                "SourceRevision is unavailable or differs from the requested revision"
            )
        policy_doc = self._store.resolve_policy(policy.policy_id, policy.policy_version)
        if (
            policy_doc["status"] != "eligible"
            or policy_doc["_revision_digest"] != policy.digest
        ):
            raise LifecycleError(
                "AcquisitionPolicy is unavailable or differs from the requested revision"
            )
        build = self._store.resolve_build(build_id, runtime_image_digest)
        verified = self._verify_registered_build(build, actor_id=actor_id)
        if verified["manifest_digest"] != build["manifest_digest"]:
            raise LifecycleError(
                "registered BotBuild bytes no longer match their immutable identity"
            )
        manifest = verified["manifest"]
        if (manifest.get("source") or {}) != {
            "source_id": source.source_id,
            "source_revision": source.revision_id,
        }:
            raise LifecycleError("BotBuild is bound to another SourceRevision")
        if manifest.get("playbook_sha256") != playbook_digest.removeprefix("sha256:"):
            raise LifecycleError("BotBuild is bound to another Playbook revision")
        playbook = self._store.resolve_playbook(
            manifest["playbook_id"], playbook_digest
        )
        if (playbook["source_id"], playbook["source_revision_id"]) != (
            source.source_id,
            source.revision_id,
        ):
            raise LifecycleError(
                "registered Playbook is bound to another SourceRevision"
            )
        if contract is not None:
            try:
                self._contracts.resolve(contract)
            except Exception as error:
                raise LifecycleError(
                    "payload contract is not exactly installed"
                ) from error
        if input_contract is not None:
            try:
                self._contracts.resolve(input_contract)
            except Exception as error:
                raise LifecycleError(
                    "input contract is not exactly installed"
                ) from error
        candidate = {
            "contract_version": "bot-release-candidate/v1",
            "release_id": release_id,
            "source": {
                "source_id": source.source_id,
                "revision_id": source.revision_id,
                "digest": source.digest,
            },
            "policy": {
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
                "digest": policy.digest,
            },
            "playbook": {
                "playbook_id": manifest["playbook_id"],
                "digest": playbook_digest,
            },
            "build": {
                "build_id": manifest["build_id"],
                "manifest_digest": verified["manifest_digest"],
                "runtime_image_digest": verified["runtime_image_digest"],
            },
            "bot_id": manifest["bot_id"],
        }
        if contract is not None:
            candidate["payload_contract"] = {
                "contract_id": contract.contract_id,
                "version": contract.version,
                "digest": contract.digest,
            }
        if output_contract is not None:
            if (
                manifest.get("manifest_version") != "bot-build-manifest/v4"
                or manifest.get("output_contract") != output_contract
            ):
                raise LifecycleError(
                    "artifact output contract differs from verified BotBuild"
                )
            candidate["output_contract"] = output_contract
        if input_contract is not None:
            live_request = manifest.get("live_request")
            input_query = (
                live_request.get("input_query")
                if isinstance(live_request, dict)
                else None
            )
            if output_contract is None and (
                not isinstance(input_query, dict)
                or set(input_query) != {"parameter", "query_name"}
            ):
                raise LifecycleError(
                    "BotRelease input contract requires a parameterized BotBuild"
                )
            candidate["input_contract"] = {
                "contract_id": input_contract.contract_id,
                "version": input_contract.version,
                "digest": input_contract.digest,
            }
            candidate["input_request"] = (
                {"method": "GET", "url": live_request["url"]}
                if output_contract is not None
                else {
                    "method": "GET",
                    "url": live_request["url"],
                    "input_query": input_query,
                }
            )
        approved = self._release_document(
            candidate, status="approved", actor_id=actor_id
        )
        try:
            validate_live_admission(
                # BC15's conformance helper uses its historical canonical_json
                # digest convention. Owner documents retained for authority
                # publication use RFC 8785; project only digest fields for this
                # non-authoritative structural check.
                source_revision=_with_digest(
                    _without_internal_fields(source_doc),
                    "configuration_digest",
                    digest_fn=_bc15_digest,
                ),
                policy=_with_digest(
                    _without_internal_fields(policy_doc),
                    "policy_digest",
                    digest_fn=_bc15_digest,
                ),
                release=_with_digest(
                    approved, "release_digest", digest_fn=_bc15_digest
                ),
                build_manifest=manifest,
                requested_urls=(manifest["live_request"]["url"],),
            )
        except Exception as error:
            raise LifecycleError(
                "release candidate dependencies failed BC15 live admission conformance"
            ) from error
        self._validate_release_schema(approved)
        candidate_digest = canonical_digest(candidate)
        self._store.create_release(release_id, candidate, candidate_digest, actor_id)
        return {
            "state": "pending",
            "candidate": candidate,
            "candidate_digest": candidate_digest,
        }

    def approve_release(self, release_id: str, *, actor_id: str) -> dict[str, Any]:
        actor_id = _actor(actor_id)
        evidence_ref = self._approval.assert_can_approve(actor_id, release_id)
        existing = self._store.resolve_release(release_id, require_eligible=False)
        if existing["state"] == "approved":
            if existing["approved_by"] != actor_id:
                raise LifecycleError(
                    "approved release cannot be re-approved by another actor"
                )
            return existing["approved_release"]
        if existing["state"] != "pending":
            raise LifecycleError("only a pending release candidate can be approved")
        document = self._release_document(
            existing["candidate"],
            status="approved",
            actor_id=actor_id,
            evidence_ref=evidence_ref,
        )
        self._validate_release_schema(document)
        self._store.approve_release(release_id, document, actor_id, evidence_ref)
        return document

    def revoke_release(self, release_id: str, *, actor_id: str) -> None:
        actor_id = _actor(actor_id)
        evidence_ref = self._approval.assert_can_approve(actor_id, release_id)
        self._store.revoke_release(release_id, actor_id, evidence_ref)

    def resolve_eligible_release(self, release_id: str) -> dict[str, Any]:
        resolved = self._store.resolve_release(release_id, require_eligible=True)
        document = resolved["approved_release"]
        source = self._store.resolve_source(
            document["source_id"], document["source_revision_id"]
        )
        policy = self._store.resolve_policy(
            document["policy_id"], document["policy_version"]
        )
        if source["status"] != "eligible" or policy["status"] != "eligible":
            raise LifecycleError("BotRelease dependency is not currently eligible")
        if source["_revision_digest"] != resolved["candidate"]["source"]["digest"]:
            raise LifecycleError("BotRelease SourceRevision digest no longer matches")
        if policy["_revision_digest"] != resolved["candidate"]["policy"]["digest"]:
            raise LifecycleError(
                "BotRelease AcquisitionPolicy digest no longer matches"
            )
        build = self._store.resolve_build(
            document["build_id"], document["runtime_image_digest"]
        )
        verified = self._verify_registered_build(build, actor_id="release-resolution")
        if (
            verified["manifest_digest"] != document["build_manifest_digest"]
            or verified["runtime_image_digest"] != document["runtime_image_digest"]
        ):
            raise LifecycleError(
                "BotRelease BotBuild or runtime identity is unavailable"
            )
        contract = document.get("payload_contract")
        if document.get("contract_version") != "bot-release/v4":
            try:
                self._contracts.resolve(
                    ContractReference(
                        contract["contract_id"], contract["version"], contract["digest"]
                    )
                )
            except Exception as error:
                raise LifecycleError(
                    "BotRelease payload contract is unavailable"
                ) from error
        if document.get("contract_version") in {"bot-release/v3", "bot-release/v4"}:
            input_contract = document.get("input_contract")
            try:
                if not isinstance(input_contract, dict):
                    raise TypeError("input contract missing")
                input_request = document.get("input_request")
                expected_request = verified["manifest"].get("live_request")
                if input_request != expected_request:
                    raise TypeError("input request differs from verified build")
                if document.get("contract_version") == "bot-release/v4" and (
                    document.get("output_contract")
                    != verified["manifest"].get("output_contract")
                    or contract is not None
                ):
                    raise TypeError(
                        "raw artifact output contract differs from verified build"
                    )
                self._contracts.resolve(
                    ContractReference(
                        input_contract["contract_id"],
                        input_contract["version"],
                        input_contract["digest"],
                    )
                )
            except Exception as error:
                raise LifecycleError(
                    "BotRelease input contract is unavailable"
                ) from error
        return resolved

    def inspect_release(self, release_id: str) -> dict[str, Any]:
        """Resolve immutable release history and current eligibility state."""
        return self._store.resolve_release(release_id, require_eligible=False)

    def _verify_registered_build(
        self, build: dict[str, Any], *, actor_id: str
    ) -> dict[str, Any]:
        verified = self.register_verified_build(
            build["artifact_path"],
            runtime_image_digest=build["runtime_image_digest"],
            actor_id=actor_id,
        )
        if verified["manifest_digest"] != build["manifest_digest"]:
            raise LifecycleError(
                "registered BotBuild bytes no longer match their immutable identity"
            )
        return verified

    def publish_authority_snapshot(
        self, *, now: datetime | None = None
    ) -> tuple[bytes, Any]:
        publication_time = now or datetime.now(UTC)
        if publication_time.tzinfo is None:
            raise LifecycleError("authority issue time must include a timezone")
        publication_time = publication_time.astimezone(UTC)
        pending = self._store.pending_authority(self._authority_id)
        current_snapshot = self._authority_registry.current_snapshot(self._authority_id)
        if pending is not None:
            pending_envelope = dict(pending["signed_envelope"])
            pending_snapshot = pending_envelope.get("snapshot")
            if not isinstance(pending_snapshot, dict):
                raise LifecycleError("pending authority snapshot is invalid")
            pending_generation = int(pending["generation"])
            accepted_digest = (
                "sha256:" + hashlib.sha256(rfc8785.dumps(pending_snapshot)).hexdigest()
            )
            if (
                current_snapshot is not None
                and current_snapshot[0] == pending_generation
            ):
                if current_snapshot[1] != accepted_digest:
                    raise LifecycleError(
                        "BC04 accepted a conflicting authority snapshot generation"
                    )
                envelope_bytes = rfc8785.dumps(pending_envelope)
                self._store.mark_authority_installed(
                    self._authority_id, pending_generation
                )
                return envelope_bytes, None
            if (
                current_snapshot is not None
                and current_snapshot[0] > pending_generation
            ):
                raise LifecycleError(
                    "pending authority snapshot is older than the BC04 accepted head"
                )
            expiry = _authority_expiry(pending_snapshot)
            if expiry <= publication_time:
                envelope_digest = (
                    "sha256:"
                    + hashlib.sha256(rfc8785.dumps(pending_envelope)).hexdigest()
                )
                self._store.supersede_expired_authority(
                    self._authority_id, pending_generation, envelope_digest
                )
                pending = None
        if pending is None:
            current = current_snapshot[0] if current_snapshot is not None else 0
            latest = self._store.latest_authority_generation(self._authority_id)
            generation = max(current, latest) + 1
            snapshot = self._authority_document(generation, publication_time)
            snapshot_digest = canonical_digest(
                {
                    key: value
                    for key, value in snapshot.items()
                    if key != "snapshot_digest"
                }
            )
            snapshot["snapshot_digest"] = snapshot_digest
            signed_bytes = rfc8785.dumps(snapshot)
            envelope = {
                "snapshot": snapshot,
                "key_id": self._signer.key_id,
                "signature": self._signer.sign(signed_bytes),
            }
            envelope_digest = (
                "sha256:" + hashlib.sha256(rfc8785.dumps(envelope)).hexdigest()
            )
            pending = self._store.prepare_authority(
                self._authority_id,
                generation,
                snapshot_digest,
                envelope_digest,
                envelope,
            )
        envelope_doc = dict(pending["signed_envelope"])
        envelope_bytes = rfc8785.dumps(envelope_doc)
        authority = self._authority_registry.install(
            envelope_bytes, self._trusted_public_keys
        )
        self._store.mark_authority_installed(
            self._authority_id, int(pending["generation"])
        )
        return envelope_bytes, authority

    def _authority_document(
        self, generation: int, issued_at: datetime
    ) -> dict[str, Any]:
        if issued_at.tzinfo is None:
            raise LifecycleError("authority issue time must include a timezone")
        issued_at = issued_at.astimezone(UTC)
        source_revisions = self._store.list_sources()
        policies = self._store.list_policies()
        releases = []
        for item in self._store.list_releases():
            document = dict(item["document"])
            projected_state = "approved" if item["state"] == "approved" else "revoked"
            releases.append(
                _with_digest(document, "release_digest", status=projected_state)
            )
        snapshot: dict[str, Any] = {
            "contract_version": "live-authority-snapshot/v1",
            "authority_id": self._authority_id,
            "generation": generation,
            "issued_at": issued_at.isoformat(),
            "expires_at": (issued_at + timedelta(hours=2)).isoformat(),
            "source_revisions": source_revisions,
            "policies": policies,
            "releases": releases,
        }
        return snapshot

    @staticmethod
    def _release_document(
        candidate: dict[str, Any],
        *,
        status: str,
        actor_id: str,
        evidence_ref: str | None = None,
    ) -> dict[str, Any]:
        source = candidate["source"]
        policy = candidate["policy"]
        build = candidate["build"]
        payload = candidate.get("payload_contract")
        document = {
            "contract_version": (
                "bot-release/v4"
                if "output_contract" in candidate
                else "bot-release/v3"
                if "input_contract" in candidate
                else "bot-release/v2"
            ),
            "release_id": candidate["release_id"],
            "status": status,
            "bot_id": candidate["bot_id"],
            "build_id": build["build_id"],
            "build_manifest_digest": build["manifest_digest"],
            "backend_id": "scrapy-live-http",
            "backend_version": "1.0.0",
            "runtime_profile_id": "scrapy-live-http-v1",
            "runtime_image_digest": build["runtime_image_digest"],
            "source_id": source["source_id"],
            "source_revision_id": source["revision_id"],
            "policy_id": policy["policy_id"],
            "policy_version": policy["policy_version"],
            "compiler_compatibility": "scrapy-live-http/v1",
            "approved_at": datetime.now(UTC).isoformat(),
            "approval_evidence_ref": evidence_ref
            or f"operator://{actor_id}/release/{candidate['release_id']}",
        }
        if payload is not None:
            document["payload_contract"] = payload
        if "output_contract" in candidate:
            document["output_contract"] = candidate["output_contract"]
        if "input_contract" in candidate:
            document["input_contract"] = candidate["input_contract"]
            document["input_request"] = candidate["input_request"]
        return _with_digest(document, "release_digest")

    @staticmethod
    def _validate_release_schema(document: dict[str, Any]) -> None:
        schema_path = {
            "bot-release/v4": _RELEASE_SCHEMA_V4,
            "bot-release/v3": _RELEASE_SCHEMA_V3,
        }.get(document.get("contract_version"), _RELEASE_SCHEMA)
        schema = json.loads(schema_path.read_bytes())
        try:
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(
                document
            )
        except ValidationError as error:
            raise LifecycleError(
                "BotRelease document is structurally invalid"
            ) from error


def _strict_schema_document(
    document: dict[str, Any], schema_path: Path, label: str
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise LifecycleError(f"{label} document must be an object")
    try:
        encoded = rfc8785.dumps(document)
    except Exception as error:
        raise LifecycleError(f"{label} document is not strict JSON") from error
    if len(encoded) > 1_000_000:
        raise LifecycleError(f"{label} document exceeds the supported size")
    schema = json.loads(schema_path.read_bytes())
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    except ValidationError as error:
        raise LifecycleError(f"{label} document is structurally invalid") from error
    return dict(document)


def _path_is_within(path: str, prefix: str) -> bool:
    if prefix == "/":
        return path.startswith("/")
    normalized = prefix.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")


def _actor(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}", value
    ):
        raise LifecycleError("operator identity is invalid")
    return value


def _authority_expiry(snapshot: dict[str, Any]) -> datetime:
    value = snapshot.get("expires_at")
    if not isinstance(value, str):
        raise LifecycleError("authority snapshot expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise LifecycleError("authority snapshot expiry is invalid") from error
    if parsed.tzinfo is None:
        raise LifecycleError("authority snapshot expiry must include a timezone")
    return parsed.astimezone(UTC)


def _with_digest(
    document: dict[str, Any],
    field: str,
    *,
    digest_fn: Any = immutable_document_digest,
    **changes: Any,
) -> dict[str, Any]:
    value = {**document, **changes}
    value[field] = ""
    value[field] = digest_fn(value, field)
    return value


def _bc15_digest(document: dict[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in document.items() if key != field}
    return "sha256:" + hashlib.sha256(canonical_json(unsigned)).hexdigest()


def _without_internal_fields(document: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if not key.startswith("_")}
