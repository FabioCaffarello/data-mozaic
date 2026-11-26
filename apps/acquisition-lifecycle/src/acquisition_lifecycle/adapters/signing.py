"""Ed25519 authority signing adapter; key material stays in operator composition."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class Ed25519AuthoritySigner:
    def __init__(self, key_id: str, private_key: Ed25519PrivateKey):
        if not key_id or len(key_id) > 128:
            raise ValueError("authority signer key identity is invalid")
        self.key_id = key_id
        self._private_key = private_key

    def sign(self, canonical_snapshot: bytes) -> str:
        return (
            base64.urlsafe_b64encode(self._private_key.sign(canonical_snapshot))
            .decode("ascii")
            .rstrip("=")
        )
