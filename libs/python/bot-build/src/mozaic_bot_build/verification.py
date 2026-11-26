"""Stdlib-only structural and integrity checks for BotBuild directories."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 1_000_000
_MAX_GENERATED_FILES = 1_000
_MAX_BUILD_FILE_BYTES = 16 * 1024 * 1024
_MAX_BUILD_TOTAL_BYTES = 64 * 1024 * 1024


class BotBuildVerificationError(ValueError):
    """A BotBuild is malformed, unsafe, or differs from its expected identity."""


@dataclass(frozen=True)
class VerifiedBotBuild:
    root: Path
    manifest: dict[str, Any]
    manifest_digest: str


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def derive_bot_id(playbook_id: str, source_id: str) -> str:
    """Derive a stable logical identity that is unique across source namespaces."""
    if not isinstance(playbook_id, str) or not playbook_id:
        raise BotBuildVerificationError(
            "[BUILD_IDENTITY_INVALID] playbook_id is required"
        )
    if not isinstance(source_id, str) or not source_id:
        raise BotBuildVerificationError(
            "[BUILD_IDENTITY_INVALID] source_id is required"
        )
    identity = {"playbook_id": playbook_id, "source_id": source_id}
    return f"bot-{hashlib.sha256(canonical_json(identity)).hexdigest()}"


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BotBuildVerificationError(
                "[BUILD_MANIFEST_DUPLICATE_KEY] duplicate key"
            )
        result[key] = value
    return result


def _safe_root(root: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise BotBuildVerificationError(
            "[BUILD_DIRECTORY_INVALID] root is not a directory"
        )
    # Resolve host-level aliases (for example macOS /var -> /private/var),
    # then reject symlinks inside the artifact itself during the exact-tree scan.
    return root.resolve(strict=True)


def _safe_relative(name: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise BotBuildVerificationError("[BUILD_FILE_UNSAFE] invalid relative path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BotBuildVerificationError("[BUILD_FILE_UNSAFE] invalid relative path")
    return path


def _verify_runtime_identity(root: Path, identity: Any) -> None:
    if not isinstance(identity, dict):
        raise BotBuildVerificationError(
            "[BUILD_RUNTIME_UNSUPPORTED] invalid runtime identity"
        )
    package = identity.get("package")
    version = identity.get("version")
    files = identity.get("files")
    identity_digest = identity.get("sha256")
    if (
        not isinstance(package, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", package)
        or not isinstance(version, str)
        or not version
        or not isinstance(files, dict)
        or not files
        or not isinstance(identity_digest, str)
        or not _DIGEST.fullmatch(identity_digest)
    ):
        raise BotBuildVerificationError(
            "[BUILD_RUNTIME_UNSUPPORTED] invalid runtime identity"
        )
    if hashlib.sha256(canonical_json(files)).hexdigest() != identity_digest:
        raise BotBuildVerificationError(
            "[BUILD_RUNTIME_IDENTITY_INVALID] aggregate digest differs"
        )
    if len(files) > _MAX_GENERATED_FILES:
        raise BotBuildVerificationError(
            "[BUILD_SIZE_LIMIT_EXCEEDED] runtime file count exceeds verification limits"
        )

    runtime_parent = root / "runtime"
    if runtime_parent.is_symlink() or not runtime_parent.is_dir():
        raise BotBuildVerificationError(
            "[BUILD_RUNTIME_MISSING] bundled runtime directory is missing or unsafe"
        )
    runtime_root = runtime_parent / package
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise BotBuildVerificationError(
            "[BUILD_RUNTIME_MISSING] bundled runtime is missing"
        )
    expected_names: set[str] = set()
    actual_paths = list(runtime_root.iterdir())
    if len(actual_paths) > _MAX_GENERATED_FILES:
        raise BotBuildVerificationError(
            "[BUILD_SIZE_LIMIT_EXCEEDED] runtime file count exceeds verification limits"
        )
    actual_names = {path.name for path in actual_paths}
    for name in files:
        relative = _safe_relative(name)
        if len(relative.parts) != 1 or relative.name in expected_names:
            raise BotBuildVerificationError(
                "[BUILD_RUNTIME_PATH_UNSAFE] runtime path is unsafe"
            )
        expected_names.add(relative.name)
    if actual_names != expected_names:
        raise BotBuildVerificationError(
            "[BUILD_RUNTIME_FILE_SET_MISMATCH] runtime file set differs"
        )

    actual: dict[str, str] = {}
    total_bytes = 0
    for name, expected in sorted(files.items()):
        relative = _safe_relative(name)
        if not isinstance(expected, str) or not _DIGEST.fullmatch(expected):
            raise BotBuildVerificationError(
                "[BUILD_RUNTIME_IDENTITY_INVALID] file digest is invalid"
            )
        path = runtime_root / relative.name
        if path.is_symlink() or not path.is_file():
            raise BotBuildVerificationError(f"[BUILD_RUNTIME_FILE_MISSING] {name}")
        size = path.stat().st_size
        total_bytes += size
        if size > _MAX_BUILD_FILE_BYTES or total_bytes > _MAX_BUILD_TOTAL_BYTES:
            raise BotBuildVerificationError(
                "[BUILD_SIZE_LIMIT_EXCEEDED] runtime exceeds verification limits"
            )
        file_digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                file_digest.update(chunk)
        actual[name] = file_digest.hexdigest()
        if actual[name] != expected:
            raise BotBuildVerificationError(f"[BUILD_RUNTIME_DIGEST_MISMATCH] {name}")

    if hashlib.sha256(canonical_json(actual)).hexdigest() != identity_digest:
        raise BotBuildVerificationError(
            "[BUILD_RUNTIME_DIGEST_MISMATCH] aggregate runtime digest differs"
        )


def verify_bot_build(
    root: Path,
    expected_manifest_digest: str,
    supported_versions: Collection[str] = (
        "bot-build-manifest/v1",
        "bot-build-manifest/v2",
        "bot-build-manifest/v3",
    ),
) -> VerifiedBotBuild:
    """Verify manifest identity and the exact immutable file set without importing code."""
    if not isinstance(expected_manifest_digest, str) or not _DIGEST.fullmatch(
        expected_manifest_digest
    ):
        raise BotBuildVerificationError(
            "[BUILD_MANIFEST_DIGEST_INVALID] expected digest is invalid"
        )
    safe_root = _safe_root(root)
    manifest_path = safe_root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BotBuildVerificationError(
            "[BUILD_MANIFEST_MISSING] manifest.json is missing or unsafe"
        )
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise BotBuildVerificationError(
            "[BUILD_MANIFEST_INVALID] manifest exceeds 1 MB"
        )
    raw = manifest_path.read_bytes()
    manifest_digest = hashlib.sha256(raw).hexdigest()
    if manifest_digest != expected_manifest_digest:
        raise BotBuildVerificationError(
            "[BUILD_MANIFEST_DIGEST_MISMATCH] manifest digest differs"
        )
    try:
        manifest = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise BotBuildVerificationError(f"[BUILD_MANIFEST_INVALID] {error}") from error
    if not isinstance(manifest, dict):
        raise BotBuildVerificationError(
            "[BUILD_MANIFEST_INVALID] manifest root must be an object"
        )
    version = manifest.get("manifest_version")
    if version not in supported_versions:
        raise BotBuildVerificationError(
            "[BUILD_VERSION_UNSUPPORTED] manifest version is unsupported"
        )
    if version in {"bot-build-manifest/v2", "bot-build-manifest/v3"}:
        allowed_v2_keys = {
            "manifest_version",
            "playbook_id",
            "bot_id",
            "source",
            "playbook_version",
            "playbook_sha256",
            "ir_version",
            "ir_sha256",
            "backend_id",
            "backend_version",
            "capability_profile_id",
            "capability_profile_sha256",
            "compiler_version",
            "runtime_identity",
            "dependency_identity",
            "compatibility",
            "toolchain",
            "dependency_lock_sha256",
            "generated_files",
            "input_fixtures",
            "live_request",
            "build_id",
        }
        playbook_id = manifest.get("playbook_id")
        source = manifest.get("source")
        dependency = manifest.get("dependency_identity")
        compatibility = manifest.get("compatibility")
        toolchain = manifest.get("toolchain")
        playbook_digest = manifest.get("playbook_sha256")
        required_v2_keys = allowed_v2_keys - {"live_request"}
        if (
            not set(manifest) <= allowed_v2_keys
            or not required_v2_keys <= set(manifest)
            or not isinstance(playbook_id, str)
            or not playbook_id
            or not isinstance(source, dict)
            or not isinstance(source.get("source_id"), str)
            or not source.get("source_id")
            or not isinstance(source.get("source_revision"), str)
            or not source.get("source_revision")
            or manifest.get("bot_id") != derive_bot_id(playbook_id, source["source_id"])
            or not isinstance(manifest.get("ir_version"), str)
            or not isinstance(manifest.get("ir_sha256"), str)
            or not _DIGEST.fullmatch(manifest["ir_sha256"])
            or not isinstance(manifest.get("backend_id"), str)
            or not isinstance(manifest.get("backend_version"), str)
            or not isinstance(manifest.get("capability_profile_id"), str)
            or not isinstance(manifest.get("capability_profile_sha256"), str)
            or not _DIGEST.fullmatch(manifest["capability_profile_sha256"])
            or not isinstance(manifest.get("compiler_version"), str)
            or not isinstance(dependency, dict)
            or not isinstance(dependency.get("lockfile_sha256"), str)
            or not _DIGEST.fullmatch(dependency["lockfile_sha256"])
            or dependency.get("lockfile_sha256")
            != manifest.get("dependency_lock_sha256")
            or not isinstance(dependency.get("packages"), dict)
            or not dependency["packages"]
            or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in dependency["packages"].items()
            )
            or not isinstance(compatibility, dict)
            or not isinstance(compatibility.get("worker_contract"), str)
            or compatibility.get("execution_mode")
            not in {"fixture-only", "live-authorized"}
            or (
                compatibility.get("execution_mode") == "live-authorized"
                and (
                    not isinstance(compatibility.get("runtime_profile"), str)
                    or not compatibility["runtime_profile"]
                )
            )
            or not isinstance(compatibility.get("python_minor"), str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+", compatibility["python_minor"])
            or not isinstance(compatibility.get("target_platform"), str)
            or not re.fullmatch(
                r"[a-z0-9_-]+/[a-z0-9_-]+", compatibility["target_platform"]
            )
            or not isinstance(toolchain, dict)
            or not toolchain
            or (
                playbook_digest is not None
                and (
                    not isinstance(playbook_digest, str)
                    or not _DIGEST.fullmatch(playbook_digest)
                )
            )
            or not isinstance(manifest.get("input_fixtures"), list)
            or (
                compatibility.get("execution_mode") == "fixture-only"
                and (not manifest["input_fixtures"] or "live_request" in manifest)
            )
            or (
                compatibility.get("execution_mode") == "live-authorized"
                and (
                    manifest["input_fixtures"]
                    or not isinstance(manifest.get("live_request"), dict)
                )
            )
        ):
            raise BotBuildVerificationError(
                f"[BUILD_MANIFEST_INVALID] {version.rsplit('/', 1)[-1]} identities are incomplete"
            )
        if version == "bot-build-manifest/v3":
            live_request = manifest.get("live_request")
            input_query = (
                live_request.get("input_query")
                if isinstance(live_request, dict)
                else None
            )
            try:
                parsed_url = urlsplit(live_request.get("url", ""))
                valid_url = (
                    parsed_url.scheme == "https"
                    and bool(parsed_url.netloc)
                    and not parsed_url.username
                    and not parsed_url.password
                    and not parsed_url.query
                    and not parsed_url.fragment
                )
            except ValueError:
                valid_url = False
            if (
                compatibility.get("execution_mode") != "live-authorized"
                or set(live_request or {}) != {"method", "url", "input_query"}
                or live_request.get("method") != "GET"
                or not valid_url
                or not isinstance(input_query, dict)
                or set(input_query) != {"parameter", "query_name"}
                or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value)
                    for value in input_query.values()
                )
            ):
                raise BotBuildVerificationError(
                    "[BUILD_MANIFEST_INVALID] v3 live input binding is invalid"
                )
        for fixture in manifest["input_fixtures"]:
            if (
                not isinstance(fixture, dict)
                or not isinstance(fixture.get("url"), str)
                or not isinstance(fixture.get("evidence_ref"), str)
                or not isinstance(fixture.get("sha256"), str)
                or not _DIGEST.fullmatch(fixture["sha256"])
                or not isinstance(fixture.get("size_bytes"), int)
                or isinstance(fixture.get("size_bytes"), bool)
                or fixture["size_bytes"] < 0
                or not isinstance(fixture.get("source_id"), str)
                or fixture["source_id"] != source["source_id"]
                or fixture.get("source_revision") != source["source_revision"]
            ):
                raise BotBuildVerificationError(
                    "[BUILD_MANIFEST_INVALID] v2 fixture identity is incomplete"
                )

    files = manifest.get("generated_files")
    if not isinstance(files, dict) or not files or len(files) > _MAX_GENERATED_FILES:
        raise BotBuildVerificationError(
            "[BUILD_MANIFEST_INVALID] generated_files must be a non-empty object"
        )
    expected_names: set[str] = set()
    total_bytes = 0
    for name, expected in sorted(files.items()):
        relative = _safe_relative(name)
        normalized = relative.as_posix()
        if normalized == "manifest.json" or normalized in expected_names:
            raise BotBuildVerificationError(
                "[BUILD_FILE_UNSAFE] duplicate or reserved path"
            )
        expected_names.add(normalized)
        if not isinstance(expected, str) or not _DIGEST.fullmatch(expected):
            raise BotBuildVerificationError(
                "[BUILD_MANIFEST_INVALID] generated file digest is invalid"
            )

    _verify_runtime_identity(safe_root, manifest.get("runtime_identity"))
    actual_names: set[str] = set()
    for directory, dirnames, filenames in os.walk(safe_root, followlinks=False):
        directory_path = Path(directory)
        for dirname in tuple(dirnames):
            child = directory_path / dirname
            if child.is_symlink():
                raise BotBuildVerificationError(
                    "[BUILD_FILE_UNSAFE] build contains a symlink"
                )
        for filename in filenames:
            path = directory_path / filename
            if path.is_symlink():
                raise BotBuildVerificationError(
                    "[BUILD_FILE_UNSAFE] build contains a symlink"
                )
            relative = path.relative_to(safe_root).as_posix()
            if relative != "manifest.json":
                actual_names.add(relative)
    if actual_names != expected_names:
        raise BotBuildVerificationError(
            "[BUILD_FILE_SET_MISMATCH] generated file set differs"
        )

    for name, expected in sorted(files.items()):
        relative = _safe_relative(name)
        path = safe_root.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise BotBuildVerificationError(f"[BUILD_FILE_MISSING] {name}")
        size = path.stat().st_size
        total_bytes += size
        if size > _MAX_BUILD_FILE_BYTES or total_bytes > _MAX_BUILD_TOTAL_BYTES:
            raise BotBuildVerificationError(
                "[BUILD_SIZE_LIMIT_EXCEEDED] build exceeds verification limits"
            )
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise BotBuildVerificationError(f"[BUILD_DIGEST_MISMATCH] {name}")

    core = {key: value for key, value in manifest.items() if key != "build_id"}
    actual_build_id = hashlib.sha256(canonical_json(core)).hexdigest()
    if manifest.get("build_id") != actual_build_id:
        raise BotBuildVerificationError(
            "[BUILD_ID_MISMATCH] build_id does not match contents"
        )
    return VerifiedBotBuild(safe_root, manifest, manifest_digest)
