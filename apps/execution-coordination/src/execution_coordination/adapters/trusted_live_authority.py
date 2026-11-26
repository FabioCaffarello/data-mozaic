"""Verify the operator-managed authority snapshot used by BC04 admission.

The snapshot is an immutable, signed input owned by the local operator. A
document's ``approved`` field is only a claim; this adapter accepts it only
when it is a member of a currently trusted signed snapshot. It is not a remote
identity service or a policy decision engine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from execution_coordination.domain import CoordinationError
from execution_coordination.durable_domain import TrustedLiveBinding
from execution_coordination.durable_ports import AuthoritySnapshotRegistryPort

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTRACT_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_CONTRACT_VERSION = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CoordinationError("authority snapshot contains duplicate keys")
        result[key] = value
    return result


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise CoordinationError(f"authority snapshot {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CoordinationError(f"authority snapshot {label} is invalid") from error
    if parsed.tzinfo is None:
        raise CoordinationError(f"authority snapshot {label} must include a timezone")
    return parsed.astimezone(UTC)


class TrustedLiveAuthority:
    """A verified read-only view of one operator-approved authority snapshot."""

    def __init__(
        self,
        snapshot_bytes: bytes,
        *,
        trusted_public_keys: Mapping[str, bytes],
        minimum_generation: int,
        authority_registry: AuthoritySnapshotRegistryPort | None = None,
        now: datetime | None = None,
    ) -> None:
        if not snapshot_bytes or len(snapshot_bytes) > 2_000_000:
            raise CoordinationError("authority snapshot size is invalid")
        if minimum_generation < 1:
            raise CoordinationError("authority generation floor is invalid")
        try:
            envelope = json.loads(
                snapshot_bytes,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    CoordinationError("authority snapshot contains a non-finite number")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoordinationError("authority snapshot is not valid JSON") from error
        if not isinstance(envelope, dict) or set(envelope) != {
            "snapshot",
            "key_id",
            "signature",
        }:
            raise CoordinationError("authority snapshot envelope is invalid")
        snapshot = envelope["snapshot"]
        key_id = envelope["key_id"]
        if not isinstance(snapshot, dict) or not isinstance(key_id, str):
            raise CoordinationError("authority snapshot envelope is invalid")
        public_bytes = trusted_public_keys.get(key_id)
        if public_bytes is None or len(public_bytes) != 32:
            raise CoordinationError("authority snapshot signer is not trusted")
        try:
            encoded_signature = envelope["signature"]
            if not isinstance(encoded_signature, str):
                raise TypeError("signature encoding is invalid")
            signature = base64.b64decode(
                encoded_signature + "=" * (-len(encoded_signature) % 4),
                altchars=b"-_",
                validate=True,
            )
            signed_bytes = rfc8785.dumps(snapshot)
            Ed25519PublicKey.from_public_bytes(public_bytes).verify(
                signature, signed_bytes
            )
        except Exception as error:
            raise CoordinationError(
                "authority snapshot signature is invalid"
            ) from error
        generation = snapshot.get("generation")
        authority_id = snapshot.get("authority_id")
        if (
            snapshot.get("contract_version") != "live-authority-snapshot/v1"
            or not isinstance(authority_id, str)
            or not authority_id.strip()
            or len(authority_id) > 128
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < minimum_generation
        ):
            raise CoordinationError("authority snapshot generation is not current")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        issued_at = _parse_time(snapshot.get("issued_at"), "issued_at")
        expires_at = _parse_time(snapshot.get("expires_at"), "expires_at")
        if issued_at > current or expires_at <= current or expires_at <= issued_at:
            raise CoordinationError("authority snapshot is not currently valid")
        digest = "sha256:" + hashlib.sha256(signed_bytes).hexdigest()
        declared_digest = snapshot.get("snapshot_digest")
        snapshot_without_digest = {
            key: value for key, value in snapshot.items() if key != "snapshot_digest"
        }
        expected_digest = (
            "sha256:"
            + hashlib.sha256(rfc8785.dumps(snapshot_without_digest)).hexdigest()
        )
        if declared_digest != expected_digest:
            raise CoordinationError("authority snapshot digest is invalid")
        envelope_digest = "sha256:" + hashlib.sha256(snapshot_bytes).hexdigest()
        self._registry = authority_registry
        self._authority_id = authority_id
        self._expires_at = expires_at
        self._generation = generation
        self._digest = digest
        if authority_registry is not None:
            authority_registry.install_authority_snapshot(
                authority_id=authority_id,
                generation=generation,
                snapshot_digest=digest,
                envelope_digest=envelope_digest,
                signing_key_id=key_id,
                expires_at=expires_at,
                signed_envelope=envelope,
            )
        self._snapshot = snapshot

    def assert_durably_current(self) -> None:
        if self._registry is None:
            raise CoordinationError("authority snapshot is not durably installed")
        if self._expires_at <= datetime.now(UTC):
            raise CoordinationError("authority snapshot has expired")
        if not self._registry.authority_snapshot_is_current(
            authority_id=self._authority_id,
            generation=self._generation,
            snapshot_digest=self._digest,
        ):
            raise CoordinationError("authority snapshot is stale or revoked")

    def resolve(
        self,
        *,
        source_id: str,
        source_revision_id: str,
        policy_id: str,
        policy_version: str,
        release_id: str,
        build_id: str,
        build_manifest_digest: str,
        runtime_image_digest: str,
    ) -> TrustedLiveBinding:
        """Resolve only records present in the verified current snapshot."""
        source = self._unique_record(
            self._snapshot.get("source_revisions"),
            ("source_id", source_id),
            ("revision_id", source_revision_id),
        )
        policy = self._unique_record(
            self._snapshot.get("policies"),
            ("policy_id", policy_id),
            ("policy_version", policy_version),
        )
        release = self._unique_record(
            self._snapshot.get("releases"), ("release_id", release_id)
        )
        if source.get("status") != "eligible":
            raise CoordinationError("source revision is not currently eligible")
        if policy.get("status") != "eligible":
            raise CoordinationError("acquisition policy is not currently eligible")
        if release.get("status") != "approved":
            raise CoordinationError("BotRelease is not currently approved")
        if release.get("contract_version") not in {
            "bot-release/v2",
            "bot-release/v3",
        }:
            raise CoordinationError("BotRelease contract version is unsupported")
        contract = release.get("payload_contract")
        if not isinstance(contract, dict) or set(contract) != {
            "contract_id",
            "version",
            "digest",
        }:
            raise CoordinationError("BotRelease payload contract binding is invalid")
        if release.get("contract_version") == "bot-release/v3":
            input_contract = release.get("input_contract")
            if not isinstance(input_contract, dict) or set(input_contract) != {
                "contract_id",
                "version",
                "digest",
            }:
                raise CoordinationError("BotRelease input contract binding is invalid")
            if (
                not isinstance(input_contract.get("contract_id"), str)
                or not _CONTRACT_ID.fullmatch(input_contract["contract_id"])
                or not isinstance(input_contract.get("version"), str)
                or not _CONTRACT_VERSION.fullmatch(input_contract["version"])
                or not isinstance(input_contract.get("digest"), str)
                or not _DIGEST.fullmatch(input_contract["digest"])
            ):
                raise CoordinationError("BotRelease input contract binding is invalid")
            input_request = release.get("input_request")
            input_query = (
                input_request.get("input_query")
                if isinstance(input_request, dict)
                else None
            )
            try:
                base_url = urlsplit(
                    input_request.get("url", "")
                    if isinstance(input_request, dict)
                    else ""
                )
                valid_base = (
                    base_url.scheme == "https"
                    and bool(base_url.netloc)
                    and not base_url.username
                    and not base_url.password
                    and not base_url.query
                    and not base_url.fragment
                )
            except ValueError:
                valid_base = False
            if (
                not isinstance(input_request, dict)
                or set(input_request) != {"method", "url", "input_query"}
                or input_request.get("method") != "GET"
                or not valid_base
                or not isinstance(input_query, dict)
                or set(input_query) != {"parameter", "query_name"}
                or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value)
                    for value in input_query.values()
                )
            ):
                raise CoordinationError("BotRelease input request binding is invalid")
        if (
            not isinstance(contract.get("contract_id"), str)
            or not isinstance(contract.get("version"), str)
            or not isinstance(contract.get("digest"), str)
            or not _CONTRACT_ID.fullmatch(contract["contract_id"])
            or not _CONTRACT_VERSION.fullmatch(contract["version"])
            or not _DIGEST.fullmatch(contract["digest"])
        ):
            raise CoordinationError("BotRelease payload contract binding is invalid")
        if (policy.get("source_id"), policy.get("source_revision_id")) != (
            source_id,
            source_revision_id,
        ):
            raise CoordinationError("policy is not bound to the selected source")
        if (release.get("source_id"), release.get("source_revision_id")) != (
            source_id,
            source_revision_id,
        ) or (release.get("policy_id"), release.get("policy_version")) != (
            policy_id,
            policy_version,
        ):
            raise CoordinationError("release is not bound to the selected authority")
        if (
            release.get("build_id") != build_id
            or release.get("build_manifest_digest") != build_manifest_digest
            or release.get("backend_id") != "scrapy-live-http"
            or release.get("runtime_profile_id") != "scrapy-live-http-v1"
            or release.get("runtime_image_digest") != runtime_image_digest
        ):
            raise CoordinationError("release is incompatible with the verified build")
        for document, digest_field in (
            (source, "configuration_digest"),
            (policy, "policy_digest"),
            (release, "release_digest"),
        ):
            unsigned = {
                key: value for key, value in document.items() if key != digest_field
            }
            expected = "sha256:" + hashlib.sha256(rfc8785.dumps(unsigned)).hexdigest()
            if document.get(digest_field) != expected:
                raise CoordinationError("trusted authority record digest is invalid")
        return TrustedLiveBinding(
            source_revision=source,
            policy=policy,
            release=release,
            authority_id=self._authority_id,
            snapshot_generation=self._generation,
            snapshot_digest=self._digest,
        )

    @staticmethod
    def _unique_record(
        records: object, *identity: tuple[str, str]
    ) -> Mapping[str, Any]:
        if not isinstance(records, list):
            raise CoordinationError("authority snapshot records are invalid")
        matches = [
            record
            for record in records
            if isinstance(record, dict)
            and all(record.get(key) == value for key, value in identity)
        ]
        if len(matches) != 1:
            raise CoordinationError("authority record is unknown or ambiguous")
        return matches[0]
