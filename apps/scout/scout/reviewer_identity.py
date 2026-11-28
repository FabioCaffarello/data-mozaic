"""OS-authenticated reviewer identity for the local operator promotion CLI.

The reviewer policy is an administrator-owned mapping from a dedicated Unix
UID to a stable reviewer principal and exact SourceRevision allowlist. It is
not an IAM system; the managed deployment must keep that UID separate from the
Codex/agent UID and protect the policy file from both identities.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .domain import AuthorizationError, SourceRef

DEFAULT_REVIEWER_POLICY = Path("/etc/data-mozaic/scout/reviewer-identities.json")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


@dataclass(frozen=True)
class ReviewerIdentity:
    uid: int
    principal: str
    source_scopes: frozenset[tuple[str, str]]

    def allows(self, source: SourceRef) -> bool:
        return (source.source_id, source.revision) in self.source_scopes


class UnixReviewerIdentityProvider:
    """Resolve identity from kernel UID plus a protected reviewer policy."""

    def __init__(
        self,
        policy_path: Path = DEFAULT_REVIEWER_POLICY,
        *,
        uid_provider: Callable[[], tuple[int, int]] | None = None,
        expected_owner_uid: int = 0,
    ):
        self._path = policy_path
        self._uid_provider = uid_provider or (lambda: (os.getuid(), os.geteuid()))
        self._expected_owner_uid = expected_owner_uid

    def current(self) -> ReviewerIdentity:
        real_uid, effective_uid = self._uid_provider()
        if real_uid != effective_uid or effective_uid == 0:
            raise AuthorizationError("reviewer process identity is not eligible")
        policy = self._load_policy()
        if effective_uid == policy["agent_uid"]:
            raise AuthorizationError("agent identity cannot act as a reviewer")
        matches = [item for item in policy["reviewers"] if item["uid"] == effective_uid]
        if len(matches) != 1:
            raise AuthorizationError("Unix identity is not an authorized reviewer")
        item = matches[0]
        return ReviewerIdentity(
            uid=effective_uid,
            principal=item["principal"],
            source_scopes=frozenset(
                (scope["source_id"], scope["revision"])
                for scope in item["source_scopes"]
            ),
        )

    def _load_policy(self) -> dict:
        path = self._path
        if not path.is_absolute():
            raise AuthorizationError("reviewer identity policy is unavailable")
        try:
            leaf_metadata = path.lstat()
            if stat.S_ISLNK(leaf_metadata.st_mode):
                raise AuthorizationError("reviewer identity policy is unavailable")
            canonical_path = path.parent.resolve(strict=True) / path.name
        except OSError as error:
            raise AuthorizationError(
                "reviewer identity policy is unavailable"
            ) from error
        _validate_parent_directories(canonical_path.parent, self._expected_owner_uid)
        try:
            descriptor = os.open(
                canonical_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            raise AuthorizationError(
                "reviewer identity policy is unavailable"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            path_metadata = canonical_path.lstat()
            if (metadata.st_dev, metadata.st_ino) != (
                path_metadata.st_dev,
                path_metadata.st_ino,
            ):
                raise AuthorizationError("reviewer identity policy changed")
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != self._expected_owner_uid
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or metadata.st_size > 64_000
            ):
                raise AuthorizationError("reviewer identity policy is not protected")
            data = bytearray()
            while len(data) <= 64_000:
                chunk = os.read(descriptor, min(8192, 64_001 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > 64_000:
                raise AuthorizationError("reviewer identity policy is too large")
        finally:
            os.close(descriptor)
        try:
            value = json.loads(
                data,
                object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("non-finite number")
                ),
            )
        except Exception as error:
            raise AuthorizationError("reviewer identity policy is invalid") from error
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "agent_uid", "reviewers"}
            or value.get("version") != 1
            or not isinstance(value.get("agent_uid"), int)
            or isinstance(value.get("agent_uid"), bool)
            or value["agent_uid"] < 1
            or not isinstance(value.get("reviewers"), list)
            or len(value["reviewers"]) > 128
        ):
            raise AuthorizationError("reviewer identity policy is invalid")
        seen_uids: set[int] = set()
        seen_principals: set[str] = set()
        for reviewer in value["reviewers"]:
            if (
                not isinstance(reviewer, dict)
                or set(reviewer) != {"uid", "principal", "source_scopes"}
                or not isinstance(reviewer.get("uid"), int)
                or isinstance(reviewer.get("uid"), bool)
                or reviewer["uid"] < 1
                or reviewer["uid"] in seen_uids
                or reviewer["uid"] == value["agent_uid"]
                or not isinstance(reviewer.get("principal"), str)
                or not _IDENTIFIER.fullmatch(reviewer["principal"])
                or reviewer["principal"] in seen_principals
                or not isinstance(reviewer.get("source_scopes"), list)
                or not reviewer["source_scopes"]
            ):
                raise AuthorizationError("reviewer identity policy is invalid")
            seen_uids.add(reviewer["uid"])
            seen_principals.add(reviewer["principal"])
            scoped: set[tuple[str, str]] = set()
            for source in reviewer["source_scopes"]:
                if (
                    not isinstance(source, dict)
                    or set(source) != {"source_id", "revision"}
                    or not all(
                        isinstance(source.get(key), str)
                        and _IDENTIFIER.fullmatch(source[key])
                        for key in ("source_id", "revision")
                    )
                    or (source["source_id"], source["revision"]) in scoped
                ):
                    raise AuthorizationError("reviewer source scope is invalid")
                scoped.add((source["source_id"], source["revision"]))
        return value


class AuthenticatedReviewerAccess:
    """Bind application authorization to an OS-authenticated identity."""

    def __init__(self, identity: ReviewerIdentity):
        self._identity = identity

    def authorize(self, reviewer: str, source: SourceRef) -> None:
        if reviewer != self._identity.principal or not self._identity.allows(source):
            raise AuthorizationError("reviewer is not authorized for this proposal")


class AuthenticatedReviewerPromotion:
    """Promotion authorization port bound to the current OS reviewer identity."""

    def __init__(self, identity: ReviewerIdentity):
        self._identity = identity

    def authorize(self, principal: str, proposal_id: str, revision: int) -> str:
        del proposal_id, revision
        if principal != self._identity.principal:
            raise AuthorizationError("promotion identity does not match reviewer")
        return self._identity.principal


class ReviewerPromotionAdapter:
    """Operational facade: identity is obtained from the OS, never request data."""

    def __init__(self, identity_provider: UnixReviewerIdentityProvider, workflow):
        self._identity_provider = identity_provider
        self._workflow = workflow

    def promote(
        self,
        session_id: str,
        expected_proposal_id: str,
        expected_proposal_revision: int,
        expected_candidate_sha256: str,
        *,
        now,
    ) -> dict[str, str]:
        identity = self._identity_provider.current()
        return self._workflow.promote_for_review(
            session_id,
            identity.principal,
            expected_proposal_id=expected_proposal_id,
            expected_proposal_revision=expected_proposal_revision,
            expected_candidate_sha256=expected_candidate_sha256,
            now=now,
        )


def _unique_object(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate property")
        result[key] = value
    return result


def _validate_parent_directories(path: Path, expected_owner_uid: int) -> None:
    for directory in (path, *path.parents):
        try:
            metadata = directory.lstat()
        except OSError as error:
            raise AuthorizationError(
                "reviewer identity policy directory is unavailable"
            ) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise AuthorizationError(
                "reviewer identity policy directory is not protected"
            )
        shared_sticky = (
            metadata.st_uid == 0
            and metadata.st_mode & stat.S_ISVTX
            and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
        if metadata.st_uid not in {0, expected_owner_uid} or (
            metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not shared_sticky
        ):
            raise AuthorizationError(
                "reviewer identity policy directory is not protected"
            )
