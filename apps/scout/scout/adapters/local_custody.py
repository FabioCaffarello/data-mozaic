"""Durable local custody reference double; not a BC06 production integration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from ..domain import ArtifactRef, ConflictError, IntegrityError, SourceRef

_ARTIFACT_ID = re.compile(r"^artifact-[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LocalFileEvidenceCustody:
    """Atomic, identity-addressable filesystem double for offline development."""

    def __init__(self, root: Path, *, max_bytes: int = 2_000_000):
        if not root.is_absolute() or max_bytes < 1:
            raise ValueError("custody root must be absolute and byte limit positive")
        self.root = root
        self.max_bytes = max_bytes

    def _root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise IntegrityError("custody root cannot be a symlink")
        return self.root.resolve(strict=True)

    def _artifact_id(self, artifact_id: str) -> None:
        if not _ARTIFACT_ID.fullmatch(artifact_id):
            raise IntegrityError("invalid custody identity")

    def _atomic_write(
        self, directory: Path, target: Path, data: bytes, *, replace: bool = True
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise IntegrityError("custody directory cannot be a symlink")
        fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=directory)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temp_path, target)
            else:
                os.link(temp_path, target)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            temp_path.unlink(missing_ok=True)

    def lookup(self, artifact_id: str) -> ArtifactRef | None:
        self._artifact_id(artifact_id)
        root = self._root()
        receipt_directory = root / "receipts"
        if receipt_directory.is_symlink():
            raise IntegrityError("custody receipt directory cannot be a symlink")
        receipt_path = receipt_directory / f"{artifact_id}.json"
        try:
            if receipt_path.is_symlink():
                raise IntegrityError("custody receipt cannot be a symlink")
            raw = json.loads(receipt_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise IntegrityError("custody receipt is unreadable") from error
        if raw.get("artifact_id") != artifact_id:
            raise IntegrityError("custody receipt identity mismatch")
        if (
            not isinstance(raw.get("sha256"), str)
            or not _SHA256.fullmatch(raw["sha256"])
            or not isinstance(raw.get("size_bytes"), int)
            or raw["size_bytes"] < 0
            or not isinstance(raw.get("session_id"), str)
        ):
            raise IntegrityError("custody receipt metadata is invalid")
        return ArtifactRef(
            artifact_id=artifact_id,
            session_id=raw["session_id"],
            source=SourceRef(**raw["source"]),
            sha256=raw["sha256"],
            kind=raw["kind"],
            redaction=raw["redaction"],
            size_bytes=raw["size_bytes"],
            custody_status="available",
        )

    def store(
        self, session_id: str, source: SourceRef, content: bytes, artifact_id: str
    ) -> ArtifactRef:
        self._artifact_id(artifact_id)
        if not session_id or len(content) > self.max_bytes:
            raise IntegrityError("custody payload exceeds configured limit")
        root = self._root()
        digest = hashlib.sha256(content).hexdigest()
        if (root / "objects").is_symlink():
            raise IntegrityError("custody object directory cannot be a symlink")
        object_path = root / "objects" / digest
        if object_path.exists():
            if (
                object_path.is_symlink()
                or hashlib.sha256(object_path.read_bytes()).hexdigest() != digest
            ):
                raise IntegrityError("existing custody object failed digest check")
        else:
            self._atomic_write(root / "objects", object_path, content)
        artifact = ArtifactRef(
            artifact_id,
            session_id,
            source,
            digest,
            "http_response",
            "none",
            len(content),
            "available",
        )
        receipt_path = root / "receipts" / f"{artifact_id}.json"
        existing = self.lookup(artifact_id)
        if existing is not None:
            if existing != artifact:
                raise ConflictError(
                    "custody identity already references different bytes"
                )
            return existing
        receipt = {
            "artifact_id": artifact.artifact_id,
            "session_id": artifact.session_id,
            "source": asdict(artifact.source),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "kind": artifact.kind,
            "redaction": artifact.redaction,
        }
        try:
            self._atomic_write(
                root / "receipts",
                receipt_path,
                (
                    json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode(),
                replace=False,
            )
        except FileExistsError:
            concurrent = self.lookup(artifact_id)
            if concurrent != artifact:
                raise ConflictError("custody identity concurrently claimed")
            return concurrent
        return artifact

    def verify(self, artifact: ArtifactRef) -> bool:
        self._artifact_id(artifact.artifact_id)
        if (
            not isinstance(artifact.sha256, str)
            or not _SHA256.fullmatch(artifact.sha256)
            or not isinstance(artifact.size_bytes, int)
            or artifact.size_bytes < 0
        ):
            return False
        root = self._root()
        if (root / "objects").is_symlink():
            return False
        object_path = root / "objects" / artifact.sha256
        if object_path.is_symlink():
            return False
        try:
            body = object_path.read_bytes()
        except OSError:
            return False
        receipt = self.lookup(artifact.artifact_id)
        return bool(
            receipt == artifact
            and len(body) == artifact.size_bytes
            and hashlib.sha256(body).hexdigest() == artifact.sha256
        )
