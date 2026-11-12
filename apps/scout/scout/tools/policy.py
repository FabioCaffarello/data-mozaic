"""Local source registration and explicit, principal-bound navigation grants."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..domain import AuthorizationError, SourceRef
from .url_policy import (
    NormalizedURL,
    TargetRejected,
    intersect_path_prefixes,
    normalize_origin,
    normalize_url,
    path_prefix_contains,
)


@dataclass(frozen=True)
class OriginGrant:
    origin: str
    path_prefixes: tuple[str, ...]

    def __post_init__(self):
        canonical = normalize_origin(self.origin)
        object.__setattr__(self, "origin", canonical)
        normalized = tuple(
            sorted(
                {_normalize_prefix(canonical, prefix) for prefix in self.path_prefixes}
            )
        )
        if not normalized:
            raise AuthorizationError("origin grant has no path prefixes")
        object.__setattr__(self, "path_prefixes", normalized)


@dataclass(frozen=True)
class RegisteredSource:
    source: SourceRef
    grants: tuple[OriginGrant, ...]
    max_requests: int
    max_duration_seconds: int
    max_response_bytes: int

    def __post_init__(self):
        if not self.source.source_id or not self.source.revision:
            raise ValueError("registered source identity is required")
        _validate_grants(self.grants)
        _validate_limits(
            self.max_requests, self.max_duration_seconds, self.max_response_bytes
        )


@dataclass(frozen=True)
class ApprovedScope:
    principal: str
    source: SourceRef
    grants: tuple[OriginGrant, ...]
    expires_at: datetime
    max_requests: int
    max_duration_seconds: int
    max_response_bytes: int

    def __post_init__(self):
        if not self.principal or not self.source.source_id or not self.source.revision:
            raise ValueError("approval identity is required")
        if self.expires_at.tzinfo is None:
            raise ValueError("approval expiration must include a timezone")
        _validate_grants(self.grants)
        _validate_limits(
            self.max_requests, self.max_duration_seconds, self.max_response_bytes
        )


@dataclass(frozen=True)
class EffectiveScope:
    principal: str
    source: SourceRef
    grants: tuple[OriginGrant, ...]
    expires_at: datetime
    max_requests: int
    max_duration_seconds: int
    max_response_bytes: int

    def __post_init__(self):
        if not self.principal or not self.source.source_id or not self.source.revision:
            raise AuthorizationError("effective scope identity is required")
        if self.expires_at.tzinfo is None:
            raise AuthorizationError(
                "effective scope expiration must include a timezone"
            )
        _validate_grants(self.grants)
        _validate_limits(
            self.max_requests, self.max_duration_seconds, self.max_response_bytes
        )

    def intersect(self, other: EffectiveScope) -> EffectiveScope:
        if (self.principal, self.source) != (other.principal, other.source):
            raise AuthorizationError("cannot combine scopes from different identities")
        other_grants = {grant.origin: grant for grant in other.grants}
        grants = []
        for grant in self.grants:
            counterpart = other_grants.get(grant.origin)
            if counterpart is None:
                continue
            paths = {
                overlap
                for left in grant.path_prefixes
                for right in counterpart.path_prefixes
                if (overlap := intersect_path_prefixes(left, right)) is not None
            }
            if paths:
                grants.append(OriginGrant(grant.origin, tuple(sorted(paths))))
        if not grants:
            raise AuthorizationError(
                "current authorization no longer overlaps this run"
            )
        return EffectiveScope(
            self.principal,
            self.source,
            tuple(grants),
            min(self.expires_at, other.expires_at),
            min(self.max_requests, other.max_requests),
            min(self.max_duration_seconds, other.max_duration_seconds),
            min(self.max_response_bytes, other.max_response_bytes),
        )

    def authorize_url(self, url: NormalizedURL, now: datetime) -> None:
        if now.tzinfo is None or self.expires_at.tzinfo is None:
            raise AuthorizationError("authorization timestamps must be timezone-aware")
        if self.expires_at <= now:
            raise AuthorizationError("navigation approval expired")
        if not any(
            grant.origin == url.origin
            and any(
                path_prefix_contains(prefix, url.path) for prefix in grant.path_prefixes
            )
            for grant in self.grants
        ):
            raise AuthorizationError("target is outside the approved source scope")


def _normalize_prefix(origin: str, prefix: str) -> str:
    if not prefix.startswith("/") or "?" in prefix or "#" in prefix:
        raise AuthorizationError("registered path prefix must be an absolute path")
    try:
        normalized = normalize_url(origin + prefix)
    except TargetRejected as error:
        raise AuthorizationError("invalid registered path prefix") from error
    if normalized.query:
        raise AuthorizationError("registered path prefix cannot contain a query")
    return normalized.path


def effective_scope(
    source: RegisteredSource, approval: ApprovedScope
) -> EffectiveScope:
    """Intersect per-origin rules without creating origin/path cross-products."""
    if source.source != approval.source:
        raise AuthorizationError("approval does not match registered source revision")
    approved_by_origin = {grant.origin: grant for grant in approval.grants}
    effective = []
    for registered in source.grants:
        approved = approved_by_origin.get(registered.origin)
        if approved is None:
            continue
        intersections = {
            overlap
            for registered_prefix in registered.path_prefixes
            for approved_prefix in approved.path_prefixes
            if (overlap := intersect_path_prefixes(registered_prefix, approved_prefix))
            is not None
        }
        if intersections:
            effective.append(
                OriginGrant(registered.origin, tuple(sorted(intersections)))
            )
    if not effective:
        raise AuthorizationError(
            "approved scope has no overlap with source registration"
        )
    return EffectiveScope(
        approval.principal,
        source.source,
        tuple(effective),
        approval.expires_at,
        min(source.max_requests, approval.max_requests),
        min(source.max_duration_seconds, approval.max_duration_seconds),
        min(source.max_response_bytes, approval.max_response_bytes),
    )


class LocalAuthorizationStore:
    """Development policy loaded from a human-reviewed, local JSON file."""

    def __init__(
        self,
        sources: tuple[RegisteredSource, ...],
        approvals: tuple[ApprovedScope, ...],
    ):
        self._sources = {item.source: item for item in sources}
        self._approvals = {(item.principal, item.source): item for item in approvals}
        if len(self._sources) != len(sources) or len(self._approvals) != len(approvals):
            raise ValueError("duplicate source registration or approval")

    @classmethod
    def from_file(cls, path: Path) -> LocalAuthorizationStore:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            raise ValueError("unsupported authoring policy version")
        sources = tuple(_registered_source(item) for item in raw.get("sources", []))
        approvals = tuple(_approved_scope(item) for item in raw.get("approvals", []))
        return cls(sources, approvals)

    def authorize(
        self, principal: str, source: SourceRef, now: datetime
    ) -> EffectiveScope:
        if now.tzinfo is None:
            raise AuthorizationError("authorization time must be timezone-aware")
        registered = self._sources.get(source)
        approval = self._approvals.get((principal, source))
        if registered is None or approval is None:
            raise AuthorizationError(
                "source is not registered or approved for this principal"
            )
        if approval.expires_at <= now:
            raise AuthorizationError("navigation approval expired")
        return effective_scope(registered, approval)


class ReloadingAuthorizationStore:
    """Reload a local policy file for each authorization check and redirect hop."""

    def __init__(self, path: Path):
        self.path = path

    def authorize(
        self, principal: str, source: SourceRef, now: datetime
    ) -> EffectiveScope:
        return LocalAuthorizationStore.from_file(self.path).authorize(
            principal, source, now
        )


def _grants(raw: list[dict]) -> tuple[OriginGrant, ...]:
    grants = tuple(
        OriginGrant(item["origin"], tuple(item["path_prefixes"])) for item in raw
    )
    _validate_grants(grants)
    return grants


def _validate_grants(grants: tuple[OriginGrant, ...]) -> None:
    if not grants or len({item.origin for item in grants}) != len(grants):
        raise ValueError("each scope requires one grouped grant per origin")


def _registered_source(raw: dict) -> RegisteredSource:
    source = SourceRef(raw["source_id"], raw["revision"])
    budgets = raw["limits"]
    return RegisteredSource(
        source,
        _grants(raw["grants"]),
        _positive_int(budgets["max_requests"]),
        _positive_int(budgets["max_duration_seconds"]),
        _positive_int(budgets["max_response_bytes"]),
    )


def _approved_scope(raw: dict) -> ApprovedScope:
    source = SourceRef(raw["source_id"], raw["revision"])
    budgets = raw["limits"]
    expires_at = datetime.fromisoformat(raw["expires_at"])
    if expires_at.tzinfo is None:
        raise ValueError("approval expiration must include a timezone")
    return ApprovedScope(
        raw["principal"],
        source,
        _grants(raw["grants"]),
        expires_at,
        _positive_int(budgets["max_requests"]),
        _positive_int(budgets["max_duration_seconds"]),
        _positive_int(budgets["max_response_bytes"]),
    )


def _positive_int(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("authorization limits must be positive integers")
    return value


def _validate_limits(
    max_requests: int, max_duration_seconds: int, max_response_bytes: int
):
    for value in (max_requests, max_duration_seconds, max_response_bytes):
        _positive_int(value)
