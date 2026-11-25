"""Trusted build-time assembly of verified BotBuilds and fixture evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from mozaic_bot_build import BotBuildVerificationError, canonical_json, verify_bot_build
from yaml.tokens import AliasToken, AnchorToken, TagToken

from .model import module_identity

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST = 1_000_000
_MAX_FIXTURE_PAGES = 1_000
_MAX_FIXTURE_PAGE_BYTES = 32 * 1024 * 1024
_MAX_FIXTURE_TOTAL_BYTES = 256 * 1024 * 1024
_WORKER_CONTRACT = "scrapy-collector/v1"
_SUPPORTED_FIXTURE_RUNTIME_VERSIONS = {
    "scrapy-fixture-runtime/1.0.0",
    "scrapy-fixture-runtime/1.1.0",
}


class AssemblyError(ValueError):
    """Assembly input is invalid, incompatible, or unsafe."""


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _UniqueSafeLoader, node: yaml.MappingNode, deep: bool = False
):
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise AssemblyError(
                "[ASSEMBLY_MANIFEST_INVALID] YAML mapping keys must be strings"
            )
        if key in mapping:
            raise AssemblyError("[ASSEMBLY_DUPLICATE_KEY] duplicate YAML key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _parse_manifest(path: Path) -> tuple[dict[str, Any], bytes]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_MANIFEST:
        raise AssemblyError(
            "[ASSEMBLY_MANIFEST_INVALID] manifest is missing or too large"
        )
    raw = path.read_bytes()
    try:
        if any(
            isinstance(token, (AliasToken, AnchorToken, TagToken))
            for token in yaml.scan(raw)
        ):
            raise AssemblyError(
                "[ASSEMBLY_YAML_FEATURE_UNSUPPORTED] aliases, anchors and tags are forbidden"
            )
        document = yaml.load(raw, Loader=_UniqueSafeLoader)
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as error:
        raise AssemblyError(f"[ASSEMBLY_MANIFEST_INVALID] {error}") from error
    if (
        not isinstance(document, dict)
        or set(document) != {"catalog_version", "entries"}
        or document.get("catalog_version") != "bot-catalog-source/v1"
        or not isinstance(document.get("entries"), list)
    ):
        raise AssemblyError(
            "[ASSEMBLY_MANIFEST_INVALID] closed manifest schema mismatch"
        )
    return document, raw


def _scan_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise AssemblyError(
            "[ASSEMBLY_INPUT_INVALID] input root must be a real directory"
        )
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        if any((parent / name).is_symlink() for name in [*dirnames, *filenames]):
            raise AssemblyError("[ASSEMBLY_PATH_UNSAFE] input contains a symlink")


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssemblyError("[FIXTURE_INDEX_INVALID] duplicate JSON key")
        result[key] = value
    return result


def _copy_verified_fixtures(
    source_root: Path, destination: Path, manifest: dict[str, Any]
) -> None:
    _scan_tree(source_root)
    index_path = source_root / "index.json"
    if (
        index_path.is_symlink()
        or not index_path.is_file()
        or index_path.stat().st_size > _MAX_MANIFEST
    ):
        raise AssemblyError(
            "[FIXTURE_INDEX_INVALID] fixture index is missing or too large"
        )
    try:
        index = json.loads(
            index_path.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise AssemblyError(f"[FIXTURE_INDEX_INVALID] {error}") from error
    source = manifest["source"]
    if (
        not isinstance(index, dict)
        or index.get("complete") is not True
        or index.get("source")
        != {
            "source_id": source["source_id"],
            "revision": source["source_revision"],
        }
        or not isinstance(index.get("pages"), list)
    ):
        raise AssemblyError(
            "[FIXTURE_INDEX_INVALID] source identity or page list differs"
        )
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for page in index["pages"]:
        if not isinstance(page, dict):
            raise AssemblyError("[FIXTURE_INDEX_INVALID] page entry is invalid")
        key = (page.get("url"), page.get("evidence_ref"))
        if key in by_identity:
            raise AssemblyError("[FIXTURE_INDEX_INVALID] duplicate URL/evidence entry")
        by_identity[key] = page

    required_pages = manifest.get("input_fixtures")
    if (
        not isinstance(required_pages, list)
        or not required_pages
        or len(required_pages) > _MAX_FIXTURE_PAGES
    ):
        raise AssemblyError(
            "[FIXTURE_INDEX_INVALID] BotBuild has no fixture identities"
        )
    copied_pages = []
    total_bytes = 0
    copied_paths: dict[str, str] = {}
    for expected in required_pages:
        page = by_identity.get((expected.get("url"), expected.get("evidence_ref")))
        if page is None:
            raise AssemblyError(
                "[FIXTURE_MISSING] fixture identity is absent from index"
            )
        for field in (
            "sha256",
            "size_bytes",
            "content_type",
            "status",
            "source_id",
            "source_revision",
        ):
            expected_value = expected.get(field)
            page_value = page.get(field)
            if field == "source_revision":
                page_value = index["source"].get("revision")
            if field == "source_id":
                page_value = index["source"].get("source_id")
            if page_value != expected_value:
                raise AssemblyError(
                    "[FIXTURE_IDENTITY_MISMATCH] fixture metadata differs from BotBuild"
                )
        body_file = page.get("body_file")
        if not isinstance(body_file, str) or "\\" in body_file:
            raise AssemblyError("[FIXTURE_PATH_UNSAFE] body file path is invalid")
        relative = PurePosixPath(body_file)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise AssemblyError(
                "[FIXTURE_PATH_UNSAFE] body file path escapes fixture root"
            )
        body_path = source_root.joinpath(*relative.parts)
        if body_path.is_symlink() or not body_path.is_file():
            raise AssemblyError("[FIXTURE_BODY_MISSING] fixture body is missing")
        body = body_path.read_bytes()
        total_bytes += len(body)
        if (
            len(body) > _MAX_FIXTURE_PAGE_BYTES
            or total_bytes > _MAX_FIXTURE_TOTAL_BYTES
        ):
            raise AssemblyError(
                "[FIXTURE_SIZE_LIMIT_EXCEEDED] fixture evidence exceeds assembly limits"
            )
        if (
            len(body) != expected["size_bytes"]
            or hashlib.sha256(body).hexdigest() != expected["sha256"]
        ):
            raise AssemblyError(
                "[FIXTURE_DIGEST_MISMATCH] fixture bytes differ from BotBuild"
            )
        previous_digest = copied_paths.get(relative.as_posix())
        if previous_digest is not None and previous_digest != expected["sha256"]:
            raise AssemblyError(
                "[FIXTURE_PATH_COLLISION] fixture bodies resolve to conflicting paths"
            )
        copied_paths[relative.as_posix()] = expected["sha256"]
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        copied = dict(page)
        copied_pages.append(copied)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "index.json").write_bytes(
        canonical_json(
            {"complete": True, "source": index["source"], "pages": copied_pages}
        )
    )


def assemble_catalog(
    *,
    manifest_path: Path,
    build_root: Path,
    fixture_root: Path,
    output: Path,
    lockfile: Path,
    target_platform: str,
) -> dict[str, Any]:
    """Assemble an immutable catalog; paths are derived only from build IDs."""
    source_manifest, raw_manifest = _parse_manifest(manifest_path)
    if not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_-]+", target_platform):
        raise AssemblyError("[ASSEMBLY_TARGET_INVALID] target platform is invalid")
    _scan_tree(build_root)
    _scan_tree(fixture_root)
    if lockfile.is_symlink() or not lockfile.is_file():
        raise AssemblyError("[ASSEMBLY_LOCK_INVALID] lockfile is missing or unsafe")
    lock_digest = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    entries = source_manifest["entries"]
    parsed_entries: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_builds: set[str] = set()
    for item in entries:
        if (
            not isinstance(item, dict)
            or set(item) != {"bot_id", "build_id", "bot_build_manifest_digest", "state"}
            or not isinstance(item.get("bot_id"), str)
            or not _SHA256.fullmatch(item.get("build_id", ""))
            or not _SHA256.fullmatch(item.get("bot_build_manifest_digest", ""))
            or item.get("state") not in {"include", "disabled"}
        ):
            raise AssemblyError(
                "[ASSEMBLY_ENTRY_INVALID] catalog source entry is invalid"
            )
        pair = (item["bot_id"], item["build_id"])
        if pair in seen_pairs or item["build_id"] in seen_builds:
            raise AssemblyError(
                "[ASSEMBLY_DUPLICATE_ENTRY] duplicate bot/build identity"
            )
        seen_pairs.add(pair)
        seen_builds.add(item["build_id"])
        source_build = build_root / item["build_id"]
        try:
            verified = verify_bot_build(
                source_build,
                item["bot_build_manifest_digest"],
                supported_versions=("bot-build-manifest/v2",),
            )
        except BotBuildVerificationError as error:
            raise AssemblyError(str(error)) from error
        build = verified.manifest
        if build.get("bot_id") != item["bot_id"]:
            raise AssemblyError(
                "[ASSEMBLY_BOT_ID_MISMATCH] entry differs from BotBuild identity"
            )
        if build.get("backend_id") != "scrapy-fixture":
            raise AssemblyError(
                "[ASSEMBLY_BACKEND_UNSUPPORTED] only scrapy-fixture is accepted"
            )
        if build.get("capability_profile_id") != "scrapy-fixture-v1":
            raise AssemblyError(
                "[ASSEMBLY_PROFILE_UNSUPPORTED] capability profile is unsupported"
            )
        if build.get("dependency_identity", {}).get("lockfile_sha256") != lock_digest:
            raise AssemblyError(
                "[ASSEMBLY_DEPENDENCIES_UNSUPPORTED] BotBuild lock identity differs"
            )
        compatibility = build.get("compatibility", {})
        if (
            compatibility.get("worker_contract") != _WORKER_CONTRACT
            or compatibility.get("execution_mode") != "fixture-only"
            or compatibility.get("scrapy") != "2.20.0"
            or compatibility.get("python_minor") != "3.12"
            or compatibility.get("scrapy_runtime")
            not in _SUPPORTED_FIXTURE_RUNTIME_VERSIONS
        ):
            raise AssemblyError(
                "[ASSEMBLY_COMPATIBILITY_UNSUPPORTED] worker/runtime compatibility differs"
            )
        if compatibility.get("target_platform") != target_platform:
            raise AssemblyError(
                "[ASSEMBLY_TARGET_MISMATCH] BotBuild target differs from assembly"
            )
        parsed_entries.append((item, build, verified.manifest_digest))

    canonical_entries: list[dict[str, Any]] = []
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise AssemblyError("[ASSEMBLY_OUTPUT_EXISTS] output path must not exist")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.assembly-", dir=output.parent)
    )
    try:
        (stage / "builds").mkdir()
        (stage / "fixtures").mkdir()
        for item, build, manifest_digest in parsed_entries:
            bot_id, build_id = item["bot_id"], item["build_id"]
            state = "enabled" if item["state"] == "include" else "disabled"
            profile_path = build_root / build_id / "capability-profile.json"
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AssemblyError(
                    "[ASSEMBLY_PROFILE_INVALID] profile file is invalid"
                ) from error
            if (
                hashlib.sha256(canonical_json(profile)).hexdigest()
                != build["capability_profile_sha256"]
            ):
                raise AssemblyError(
                    "[ASSEMBLY_PROFILE_DIGEST_MISMATCH] profile bytes differ"
                )
            module_name = module_identity(bot_id, build_id)
            catalog_entry = {
                "bot_id": bot_id,
                "build_id": build_id,
                "build_manifest_digest": manifest_digest,
                "status": state,
                "module_identity": module_name,
                "spider_name": "mozaic-generated-fixture-spider",
                "playbook_id": build["playbook_id"],
                "source": build["source"],
                "ir_sha256": build["ir_sha256"],
                "backend_id": build["backend_id"],
                "backend_version": build["backend_version"],
                "capability_profile_id": build["capability_profile_id"],
                "runtime_identity": build["runtime_identity"],
                "compatibility": build["compatibility"],
                "capabilities": profile["capabilities"],
                "input_fixtures": build["input_fixtures"],
                "execution_modes": ["fixture-only"],
            }
            canonical_entries.append(catalog_entry)
            if state == "disabled":
                continue
            destination_build = stage / "builds" / build_id
            shutil.copytree(build_root / build_id, destination_build, symlinks=False)
            copied = verify_bot_build(
                destination_build,
                manifest_digest,
                supported_versions=("bot-build-manifest/v2",),
            )
            if copied.manifest["build_id"] != build_id:
                raise AssemblyError(
                    "[ASSEMBLY_BUILD_ID_MISMATCH] copied build identity differs"
                )
            _copy_verified_fixtures(
                fixture_root / build_id,
                stage / "fixtures" / build_id,
                copied.manifest,
            )
        canonical_entries.sort(key=lambda entry: (entry["bot_id"], entry["build_id"]))
        identity_entries = [
            {
                "bot_id": entry["bot_id"],
                "build_id": entry["build_id"],
                "build_manifest_digest": entry["build_manifest_digest"],
                "state": "include" if entry["status"] == "enabled" else "disabled",
                "catalog_entry_sha256": hashlib.sha256(
                    canonical_json(entry)
                ).hexdigest(),
            }
            for entry in canonical_entries
        ]
        identity_payload = {
            "assembly_version": "bot-assembly/v1",
            "catalog_source_sha256": hashlib.sha256(raw_manifest).hexdigest(),
            "dependency_lock_sha256": lock_digest,
            "worker_contract": _WORKER_CONTRACT,
            "target_platform": target_platform,
            "entries": identity_entries,
        }
        assembly_id = hashlib.sha256(canonical_json(identity_payload)).hexdigest()
        catalog_doc = {
            "catalog_version": "bot-catalog/v1",
            "worker_contract": _WORKER_CONTRACT,
            "assembly_id": assembly_id,
            "entries": canonical_entries,
        }
        catalog_bytes = canonical_json(catalog_doc)
        assembly_doc = {
            "assembly_version": "bot-assembly/v1",
            "assembly_id": assembly_id,
            "identity_payload": identity_payload,
            "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            "dependency_lock_sha256": lock_digest,
            "target_platform": target_platform,
        }
        (stage / "catalog.json").write_bytes(catalog_bytes)
        (stage / "assembly.json").write_bytes(canonical_json(assembly_doc))
        for directory, dirnames, filenames in os.walk(stage, topdown=False):
            current = Path(directory)
            for filename in filenames:
                (current / filename).chmod(0o444)
            for dirname in dirnames:
                (current / dirname).chmod(0o555)
            if current != stage:
                current.chmod(0o555)
        os.replace(stage, output)
        output.chmod(0o555)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return assembly_doc
