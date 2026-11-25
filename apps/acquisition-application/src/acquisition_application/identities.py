"""Deterministic scoped identifiers used by acquisition import."""

from __future__ import annotations

import hashlib

from acquisition_output.domain import canonical_json


def stable_identifier(prefix: str, identity: object) -> str:
    """Derive an idempotent opaque identifier from canonical intent data."""
    if not prefix or not prefix.isascii() or not prefix.replace("-", "").isalnum():
        raise ValueError("identifier prefix is invalid")
    return prefix + hashlib.sha256(canonical_json(identity)).hexdigest()
