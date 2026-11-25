"""BC04 issuer and verifier for short-lived gateway execution capabilities."""

from __future__ import annotations

import base64
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from execution_coordination.domain import CoordinationError
from execution_coordination.durable_domain import AttemptLease

_ISSUER = "urn:data-mozaic:bc04:execution-coordination"
_RECIPIENT = "urn:data-mozaic:acquisition-gateway"
_MAX_LIFETIME = timedelta(minutes=2)
_SAFE_CLAIM_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/_+\-]{0,255}$")


def _canonical(fields: Mapping[str, object]) -> bytes:
    try:
        return rfc8785.dumps(dict(fields))
    except Exception as error:
        raise CoordinationError("execution capability is not canonical JSON") from error


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CoordinationError(f"execution capability {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CoordinationError(f"execution capability {field} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CoordinationError(f"execution capability {field} must include timezone")
    return parsed.astimezone(UTC)


class ExecutionCapabilitySigner:
    """Signs bounded authorization claims; private material remains in BC04."""

    def __init__(self, private_seed: bytes, *, key_id: str) -> None:
        if not isinstance(private_seed, bytes) or len(private_seed) != 32:
            raise CoordinationError("Ed25519 private seed must contain 32 bytes")
        if not key_id or len(key_id) > 64:
            raise CoordinationError("execution capability key_id is invalid")
        self._key = Ed25519PrivateKey.from_private_bytes(private_seed)
        self._key_id = key_id

    def verify(self, capability: Mapping[str, object], *, audience: str) -> None:
        public = self._key.public_key().public_bytes_raw()
        verify_execution_capability(
            capability,
            trusted_public_keys={self._key_id: public},
            expected_audience=audience,
        )

    def issue(
        self,
        lease: AttemptLease,
        *,
        source_id: str,
        source_revision_id: str,
        policy_id: str,
        policy_version: str,
        policy_digest: str,
        release_id: str,
        build_id: str,
        build_manifest_digest: str,
        runtime_profile_id: str,
        audience: str,
        execution_deadline: datetime,
        now: datetime | None = None,
    ) -> dict[str, object]:
        current = now or datetime.now(UTC)
        if current.tzinfo is None or current.utcoffset() is None:
            raise CoordinationError("execution capability clock must be timezone-aware")
        if execution_deadline.tzinfo is None or execution_deadline.utcoffset() is None:
            raise CoordinationError("execution deadline must be timezone-aware")
        if not audience or len(audience) > 256:
            raise CoordinationError("execution capability audience is invalid")
        expiry = min(
            current.astimezone(UTC) + _MAX_LIFETIME,
            lease.lease_until.astimezone(UTC),
            lease.attempt_deadline.astimezone(UTC),
            execution_deadline.astimezone(UTC),
        )
        if expiry <= current.astimezone(UTC):
            raise CoordinationError("attempt is already expired")
        claims: dict[str, object] = {
            "contract_version": "execution-capability/v1",
            "issuer": _ISSUER,
            "recipient": _RECIPIENT,
            "algorithm": "Ed25519",
            "key_id": self._key_id,
            "audience": audience,
            "tenant_id": lease.tenant_id,
            "run_id": lease.run_id,
            "attempt_id": lease.attempt_id,
            "generation": lease.generation,
            "source_id": source_id,
            "source_revision_id": source_revision_id,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "policy_digest": policy_digest,
            "release_id": release_id,
            "build_id": build_id,
            "build_manifest_digest": build_manifest_digest,
            "runtime_profile_id": runtime_profile_id,
            "issued_at": current.astimezone(UTC).isoformat(),
            "expires_at": expiry.isoformat(),
            "execution_deadline": execution_deadline.astimezone(UTC).isoformat(),
            "capability_id": "cap-" + secrets.token_urlsafe(24),
        }
        for name, value in claims.items():
            if isinstance(value, str) and not _SAFE_CLAIM_VALUE.fullmatch(value):
                raise CoordinationError(
                    f"execution capability {name} contains unsupported text"
                )
        signature = base64.urlsafe_b64encode(self._key.sign(_canonical(claims)))
        claims["signature"] = "ed25519:" + signature.rstrip(b"=").decode("ascii")
        return claims


def verify_execution_capability(
    capability: Mapping[str, Any],
    *,
    trusted_public_keys: Mapping[str, bytes],
    expected_audience: str,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    """Verify authenticity, recipient, lifetime and closed protocol fields."""
    required = {
        "contract_version",
        "issuer",
        "recipient",
        "algorithm",
        "key_id",
        "audience",
        "tenant_id",
        "run_id",
        "attempt_id",
        "generation",
        "source_id",
        "source_revision_id",
        "policy_id",
        "policy_version",
        "policy_digest",
        "release_id",
        "build_id",
        "build_manifest_digest",
        "runtime_profile_id",
        "issued_at",
        "expires_at",
        "execution_deadline",
        "capability_id",
        "signature",
    }
    if set(capability) != required:
        raise CoordinationError("execution capability fields are invalid")
    unsigned = {key: value for key, value in capability.items() if key != "signature"}
    key_id = unsigned["key_id"]
    public_bytes = trusted_public_keys.get(key_id) if isinstance(key_id, str) else None
    signature_value = capability["signature"]
    if (
        unsigned.get("contract_version") != "execution-capability/v1"
        or unsigned.get("issuer") != _ISSUER
        or unsigned.get("recipient") != _RECIPIENT
        or unsigned.get("algorithm") != "Ed25519"
        or unsigned.get("audience") != expected_audience
        or not isinstance(public_bytes, bytes)
        or len(public_bytes) != 32
        or not isinstance(signature_value, str)
        or not signature_value.startswith("ed25519:")
    ):
        raise CoordinationError("execution capability is not trusted for this gateway")
    encoded_signature = signature_value.removeprefix("ed25519:")
    try:
        signature = base64.b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4),
            altchars=b"-_",
            validate=True,
        )
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            signature, _canonical(unsigned)
        )
    except (ValueError, TypeError, InvalidSignature) as error:
        raise CoordinationError("execution capability signature is invalid") from error
    issued_at = _time(unsigned["issued_at"], "issued_at")
    expires_at = _time(unsigned["expires_at"], "expires_at")
    execution_deadline = _time(unsigned["execution_deadline"], "execution_deadline")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise CoordinationError("execution capability clock must be timezone-aware")
    current = current.astimezone(UTC)
    if (
        issued_at > current
        or expires_at <= current
        or expires_at > execution_deadline
        or expires_at - issued_at > _MAX_LIFETIME
    ):
        raise CoordinationError("execution capability is expired or out of scope")
    if (
        not isinstance(unsigned["generation"], int)
        or isinstance(unsigned["generation"], bool)
        or unsigned["generation"] < 1
    ):
        raise CoordinationError("execution capability generation is invalid")
    for field in (
        "tenant_id",
        "run_id",
        "attempt_id",
        "source_id",
        "source_revision_id",
        "policy_id",
        "policy_version",
        "policy_digest",
        "release_id",
        "build_id",
        "build_manifest_digest",
        "runtime_profile_id",
        "capability_id",
    ):
        if not isinstance(unsigned[field], str) or not _SAFE_CLAIM_VALUE.fullmatch(
            unsigned[field]
        ):
            raise CoordinationError(f"execution capability {field} is invalid")
    return unsigned
