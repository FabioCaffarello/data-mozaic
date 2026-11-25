"""BC04 draft publication-grant signing adapter."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import UTC, datetime

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.domain import CoordinationError
from execution_coordination.durable_domain import FinalizationGrant
from execution_coordination.durable_ports import PublicationGrantSignerPort

_ISSUER = "urn:data-mozaic:bc04:execution-coordination"
_RECIPIENT = "urn:data-mozaic:bc05:acquisition-output"


def _canonical(fields: Mapping[str, object]) -> bytes:
    try:
        return rfc8785.dumps(dict(fields))
    except Exception as error:
        raise CoordinationError("publication grant is not canonical JSON") from error


class Ed25519PublicationGrantSigner(PublicationGrantSignerPort):
    """BC04 signer. Only the verification key is distributed to BC05."""

    def __init__(self, private_seed: bytes, *, key_id: str) -> None:
        if not isinstance(private_seed, bytes) or len(private_seed) != 32:
            raise CoordinationError("Ed25519 private seed must contain 32 bytes")
        if not key_id or len(key_id) > 64:
            raise CoordinationError("publication grant key_id is invalid")
        self._key = Ed25519PrivateKey.from_private_bytes(private_seed)
        self._key_id = key_id

    def issue(self, fields: Mapping[str, object]) -> FinalizationGrant:
        base = dict(fields)
        issued_at = base.get("issued_at")
        if isinstance(issued_at, str):
            try:
                parsed = datetime.fromisoformat(issued_at)
            except ValueError as error:
                raise CoordinationError(
                    "publication grant issued_at is invalid"
                ) from error
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise CoordinationError(
                    "publication grant issued_at must include a timezone"
                )
            base["issued_at"] = parsed.astimezone(UTC).isoformat()
        base.update(
            contract_version="publication-grant/v1",
            issuer=_ISSUER,
            recipient=_RECIPIENT,
            algorithm="Ed25519",
            key_id=self._key_id,
        )
        encoded_signature = (
            base64.urlsafe_b64encode(self._key.sign(_canonical(base)))
            .rstrip(b"=")
            .decode("ascii")
        )
        signature = "ed25519:" + encoded_signature
        object_fields = dict(base)
        issued_at = object_fields.get("issued_at")
        if isinstance(issued_at, str):
            object_fields["issued_at"] = datetime.fromisoformat(issued_at)
        try:
            return FinalizationGrant(**object_fields, signature=signature)  # type: ignore[arg-type]
        except TypeError as error:
            raise CoordinationError("publication grant fields are invalid") from error
