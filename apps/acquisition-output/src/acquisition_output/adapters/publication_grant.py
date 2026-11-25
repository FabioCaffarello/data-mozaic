"""BC05 verifier for the BC04-owned publication-grant draft contract."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from acquisition_output.domain import OutputError, PublicationAuthorization

_FIELDS = {
    "contract_version",
    "issuer",
    "recipient",
    "algorithm",
    "key_id",
    "tenant_id",
    "run_id",
    "attempt_id",
    "fencing_generation",
    "output_set_id",
    "manifest_digest",
    "reservation_id",
    "operation_id",
    "issued_at",
    "signature",
}
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_RESERVATION = re.compile(r"^res-[0-9a-f]{64}$")
_SIGNATURE = re.compile(r"^ed25519:([A-Za-z0-9_-]{86})$")


class Ed25519PublicationGrantVerifier:
    """Verifies BC04 signatures using public keys only."""

    def __init__(self, public_keys: Mapping[str, bytes]) -> None:
        if not public_keys or any(
            not isinstance(key_id, str)
            or not key_id
            or len(key_id) > 64
            or not isinstance(key, bytes)
            or len(key) != 32
            for key_id, key in public_keys.items()
        ):
            raise OutputError("publication grant verification keys are invalid")
        self._keys = {
            key_id: Ed25519PublicKey.from_public_bytes(bytes(key))
            for key_id, key in public_keys.items()
        }

    def verify(self, grant: Mapping[str, object]) -> PublicationAuthorization:
        if not isinstance(grant, Mapping) or set(grant) != _FIELDS:
            raise OutputError("publication grant fields do not match draft v1")
        fields = dict(grant)
        signature = fields.pop("signature")
        key_id = fields.get("key_id")
        if not isinstance(key_id, str) or key_id not in self._keys:
            raise OutputError("publication grant key is not trusted")
        match = _SIGNATURE.fullmatch(signature) if isinstance(signature, str) else None
        if match is None:
            raise OutputError("publication grant signature encoding is invalid")
        if (
            fields.get("contract_version") != "publication-grant/v1"
            or fields.get("algorithm") != "Ed25519"
            or fields.get("issuer") != "urn:data-mozaic:bc04:execution-coordination"
            or fields.get("recipient") != "urn:data-mozaic:bc05:acquisition-output"
        ):
            raise OutputError("publication grant issuer or recipient is invalid")
        for name in (
            "tenant_id",
            "output_set_id",
            "reservation_id",
            "operation_id",
        ):
            value = fields.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise OutputError(f"publication grant {name} is invalid")
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as error:
                raise OutputError(
                    "publication grant contains invalid Unicode"
                ) from error
        for name in ("run_id", "attempt_id"):
            value = fields.get(name)
            if not isinstance(value, str):
                raise OutputError(f"publication grant {name} is invalid")
            try:
                UUID(value)
            except ValueError as error:
                raise OutputError(f"publication grant {name} is invalid") from error
        generation = fields.get("fencing_generation")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or not 1 <= generation <= (1 << 53) - 1
        ):
            raise OutputError("publication grant generation is invalid")
        digest = fields.get("manifest_digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise OutputError("publication grant manifest digest is invalid")
        reservation_id = fields["reservation_id"]
        if not _RESERVATION.fullmatch(reservation_id):
            raise OutputError("publication grant reservation identity is invalid")
        issued_at = fields.get("issued_at")
        if not isinstance(issued_at, str):
            raise OutputError("publication grant issued_at is invalid")
        try:
            parsed_at = datetime.fromisoformat(issued_at)
        except ValueError as error:
            raise OutputError("publication grant issued_at is invalid") from error
        if parsed_at.tzinfo is None or parsed_at.utcoffset() is None:
            raise OutputError("publication grant issued_at must include a timezone")
        encoded = match.group(1)
        try:
            signature_bytes = base64.urlsafe_b64decode(encoded + "==")
            self._keys[key_id].verify(signature_bytes, _canonical(fields))
        except (ValueError, InvalidSignature) as error:
            raise OutputError("publication grant signature does not verify") from error
        return PublicationAuthorization(
            tenant_id=fields["tenant_id"],
            run_id=fields["run_id"],
            attempt_id=fields["attempt_id"],
            fencing_generation=generation,
            output_set_id=fields["output_set_id"],
            manifest_digest=digest,
            reservation_id=reservation_id,
            operation_id=fields["operation_id"],
        )


def _canonical(fields: Mapping[str, object]) -> bytes:
    try:
        return rfc8785.dumps(dict(fields))
    except Exception as error:
        raise OutputError("publication grant is not canonical JSON") from error
