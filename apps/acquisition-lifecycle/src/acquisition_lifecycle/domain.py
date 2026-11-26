"""BC01, BC11 and BC15 lifecycle value types used by the composition root."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import rfc8785

_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class LifecycleError(ValueError):
    """A lifecycle command violates an identity, integrity or eligibility rule."""


def canonical_digest(value: object) -> str:
    try:
        return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()
    except Exception as error:
        raise LifecycleError("lifecycle document is not canonical JSON") from error


def require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LifecycleError(f"{label} is invalid")
    return value


def require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise LifecycleError(f"{label} is invalid")
    return value


def immutable_document_digest(document: dict[str, Any], digest_field: str) -> str:
    unsigned = {key: value for key, value in document.items() if key != digest_field}
    return canonical_digest(unsigned)


@dataclass(frozen=True, slots=True)
class SourceRevisionRef:
    source_id: str
    revision_id: str
    digest: str

    def __post_init__(self) -> None:
        require_identifier(self.source_id, "source identity")
        require_identifier(self.revision_id, "source revision identity")
        require_digest(self.digest, "source revision digest")


@dataclass(frozen=True, slots=True)
class PolicyRevisionRef:
    policy_id: str
    policy_version: str
    digest: str

    def __post_init__(self) -> None:
        require_identifier(self.policy_id, "policy identity")
        if not isinstance(self.policy_version, str) or len(self.policy_version) > 64:
            raise LifecycleError("policy version is invalid")
        require_digest(self.digest, "policy digest")


@dataclass(frozen=True, slots=True)
class PlaybookRevisionRef:
    playbook_id: str
    digest: str

    def __post_init__(self) -> None:
        require_identifier(self.playbook_id, "playbook identity")
        require_digest(self.digest, "Playbook revision digest")


@dataclass(frozen=True, slots=True)
class BotBuildRef:
    build_id: str
    manifest_digest: str
    runtime_image_digest: str

    def __post_init__(self) -> None:
        require_digest("sha256:" + self.build_id, "BotBuild identity")
        require_digest(self.manifest_digest, "BotBuild manifest digest")
        require_digest(self.runtime_image_digest, "runtime image digest")
