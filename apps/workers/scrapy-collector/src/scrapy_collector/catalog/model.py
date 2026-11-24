"""Runtime parsing and exact lookup for an assembled Bot Catalog."""

from __future__ import annotations

import hashlib
import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mozaic_bot_build.verification import canonical_json

BOT_ID = re.compile(r"^bot-[0-9a-f]{64}$")
BUILD_ID = re.compile(r"^[0-9a-f]{64}$")
WORKER_CONTRACT = "scrapy-collector/v1"


def runtime_platform_id() -> str:
    system = platform.system().lower()
    machine = {"x86_64": "amd64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    return f"{system}/{machine}"


class CatalogError(ValueError):
    """The installed catalog is invalid or does not authorize a selection."""


@dataclass(frozen=True)
class CatalogEntry:
    bot_id: str
    build_id: str
    build_manifest_digest: str
    status: str
    module_identity: str
    spider_name: str
    source: dict[str, str]
    playbook_id: str
    ir_sha256: str
    backend_id: str
    backend_version: str
    capability_profile_id: str
    runtime_identity: dict[str, Any]
    compatibility: dict[str, Any]
    capabilities: tuple[str, ...]
    input_fixtures: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class BotCatalog:
    root: Path
    assembly_id: str
    dependency_lock_sha256: str
    target_platform: str
    entries: tuple[CatalogEntry, ...]

    def resolve(self, bot_id: str, build_id: str) -> CatalogEntry:
        if not BOT_ID.fullmatch(bot_id) or not BUILD_ID.fullmatch(build_id):
            raise CatalogError("[BOT_ID_INVALID] bot_id/build_id format is invalid")
        matches = [
            item
            for item in self.entries
            if item.bot_id == bot_id and item.build_id == build_id
        ]
        if not matches:
            raise CatalogError(
                "[BOT_BUILD_NOT_FOUND] exact bot_id/build_id is not installed"
            )
        entry = matches[0]
        if entry.status != "enabled":
            raise CatalogError("[BOT_BUILD_DISABLED] selected BotBuild is disabled")
        return entry

    def build_root(self, entry: CatalogEntry) -> Path:
        return self.root / "builds" / entry.build_id

    def fixture_root(self, entry: CatalogEntry) -> Path:
        return self.root / "fixtures" / entry.build_id


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError("[CATALOG_DUPLICATE_KEY] duplicate JSON key")
        result[key] = value
    return result


def load_catalog(root: Path) -> BotCatalog:
    """Load metadata and assembly identity without importing any BotBuild module."""
    root = root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise CatalogError("[CATALOG_ROOT_INVALID] catalog root must be a directory")
    catalog_path = root / "catalog.json"
    assembly_path = root / "assembly.json"
    for path in (catalog_path, assembly_path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
            raise CatalogError(
                "[CATALOG_FILE_INVALID] catalog or assembly file is unsafe"
            )
    try:
        catalog_bytes = catalog_path.read_bytes()
        assembly_bytes = assembly_path.read_bytes()
        catalog_doc = json.loads(
            catalog_bytes.decode("utf-8"), object_pairs_hook=_pairs
        )
        assembly = json.loads(assembly_bytes.decode("utf-8"), object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogError(f"[CATALOG_INVALID] {error}") from error
    if (
        canonical_json(catalog_doc) != catalog_bytes
        or canonical_json(assembly) != assembly_bytes
    ):
        raise CatalogError(
            "[CATALOG_NONCANONICAL] catalog artifacts are not canonical JSON"
        )
    if not isinstance(catalog_doc, dict) or not isinstance(assembly, dict):
        raise CatalogError("[CATALOG_INVALID] roots must be JSON objects")
    identity_payload = assembly.get("identity_payload")
    assembly_id = assembly.get("assembly_id")
    if (
        not isinstance(identity_payload, dict)
        or not isinstance(assembly_id, str)
        or hashlib.sha256(canonical_json(identity_payload)).hexdigest() != assembly_id
        or catalog_doc.get("assembly_id") != assembly_id
        or set(catalog_doc)
        != {"catalog_version", "worker_contract", "assembly_id", "entries"}
        or assembly.get("catalog_sha256") != hashlib.sha256(catalog_bytes).hexdigest()
        or catalog_doc.get("catalog_version") != "bot-catalog/v1"
        or catalog_doc.get("worker_contract") != WORKER_CONTRACT
        or not isinstance(assembly.get("target_platform"), str)
        or not re.fullmatch(r"[a-z0-9_-]+/[a-z0-9_-]+", assembly["target_platform"])
        or not isinstance(identity_payload, dict)
        or identity_payload.get("target_platform") != assembly.get("target_platform")
        or identity_payload.get("worker_contract") != WORKER_CONTRACT
        or identity_payload.get("dependency_lock_sha256")
        != assembly.get("dependency_lock_sha256")
        or set(assembly)
        != {
            "assembly_version",
            "assembly_id",
            "identity_payload",
            "catalog_sha256",
            "dependency_lock_sha256",
            "target_platform",
        }
        or set(identity_payload)
        != {
            "assembly_version",
            "catalog_source_sha256",
            "dependency_lock_sha256",
            "worker_contract",
            "target_platform",
            "entries",
        }
        or assembly.get("assembly_version") != "bot-assembly/v1"
        or identity_payload.get("assembly_version") != "bot-assembly/v1"
    ):
        raise CatalogError(
            "[ASSEMBLY_IDENTITY_MISMATCH] catalog and assembly identity differ"
        )
    entries_doc = catalog_doc.get("entries")
    identity_entries = identity_payload.get("entries")
    if (
        not isinstance(entries_doc, list)
        or not isinstance(identity_entries, list)
        or len(entries_doc) != len(identity_entries)
    ):
        raise CatalogError("[CATALOG_INVALID] entries must be a list")
    entry_identity = {}
    for identity in identity_entries:
        if (
            not isinstance(identity, dict)
            or set(identity)
            != {
                "bot_id",
                "build_id",
                "build_manifest_digest",
                "state",
                "catalog_entry_sha256",
            }
            or identity.get("state") not in {"include", "disabled"}
            or not isinstance(identity.get("catalog_entry_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity["catalog_entry_sha256"])
        ):
            raise CatalogError(
                "[ASSEMBLY_IDENTITY_MISMATCH] entry identity is malformed"
            )
        identity_pair = (identity["bot_id"], identity["build_id"])
        if identity_pair in entry_identity:
            raise CatalogError(
                "[ASSEMBLY_IDENTITY_MISMATCH] entry identity is duplicated"
            )
        entry_identity[identity_pair] = identity
    entries: list[CatalogEntry] = []
    seen: set[tuple[str, str]] = set()
    for raw in entries_doc:
        if not isinstance(raw, dict):
            raise CatalogError("[CATALOG_INVALID] entry must be an object")
        bot_id = raw.get("bot_id")
        build_id = raw.get("build_id")
        if not isinstance(bot_id, str) or not isinstance(build_id, str):
            raise CatalogError("[CATALOG_ENTRY_INVALID] bot/build ID must be a string")
        pair = (bot_id, build_id)
        expected_identity = entry_identity.get(pair)
        if (
            set(raw)
            != {
                "bot_id",
                "build_id",
                "build_manifest_digest",
                "status",
                "module_identity",
                "spider_name",
                "playbook_id",
                "source",
                "ir_sha256",
                "backend_id",
                "backend_version",
                "capability_profile_id",
                "runtime_identity",
                "compatibility",
                "capabilities",
                "input_fixtures",
                "execution_modes",
            }
            or expected_identity is None
            or expected_identity["build_manifest_digest"]
            != raw.get("build_manifest_digest")
            or expected_identity["state"]
            != ("include" if raw.get("status") == "enabled" else "disabled")
            or expected_identity["catalog_entry_sha256"]
            != hashlib.sha256(canonical_json(raw)).hexdigest()
        ):
            raise CatalogError(
                "[ASSEMBLY_IDENTITY_MISMATCH] catalog entry differs from assembly identity"
            )
        if pair in seen:
            raise CatalogError("[CATALOG_DUPLICATE_ENTRY] bot/build pair is duplicated")
        seen.add(pair)
        if (
            not isinstance(bot_id, str)
            or not BOT_ID.fullmatch(bot_id)
            or not isinstance(build_id, str)
            or not BUILD_ID.fullmatch(build_id)
            or raw.get("status") not in {"enabled", "disabled"}
            or raw.get("module_identity") != module_identity(bot_id, build_id)
            or raw.get("backend_id") != "scrapy-fixture"
            or raw.get("execution_modes") != ["fixture-only"]
            or not isinstance(raw.get("build_manifest_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw["build_manifest_digest"])
            or not isinstance(raw.get("spider_name"), str)
            or not raw["spider_name"]
            or not isinstance(raw.get("playbook_id"), str)
            or not isinstance(raw.get("ir_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", raw["ir_sha256"])
            or not isinstance(raw.get("backend_version"), str)
            or not isinstance(raw.get("capability_profile_id"), str)
        ):
            raise CatalogError(
                "[CATALOG_ENTRY_INVALID] identity or compatibility is invalid"
            )
        source = raw.get("source")
        runtime = raw.get("runtime_identity")
        compatibility = raw.get("compatibility")
        capabilities = raw.get("capabilities")
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("source_id"), str)
            or not isinstance(source.get("source_revision"), str)
            or not isinstance(runtime, dict)
            or not isinstance(compatibility, dict)
            or not isinstance(capabilities, list)
            or any(not isinstance(item, str) for item in capabilities)
            or not isinstance(raw.get("input_fixtures"), list)
            or any(not isinstance(item, dict) for item in raw["input_fixtures"])
        ):
            raise CatalogError("[CATALOG_ENTRY_INVALID] metadata is incomplete")
        entries.append(
            CatalogEntry(
                bot_id=bot_id,
                build_id=build_id,
                build_manifest_digest=raw["build_manifest_digest"],
                status=raw["status"],
                module_identity=raw["module_identity"],
                spider_name=raw["spider_name"],
                source=source,
                playbook_id=raw["playbook_id"],
                ir_sha256=raw["ir_sha256"],
                backend_id=raw["backend_id"],
                backend_version=raw["backend_version"],
                capability_profile_id=raw["capability_profile_id"],
                runtime_identity=runtime,
                compatibility=compatibility,
                capabilities=tuple(capabilities),
                input_fixtures=tuple(raw["input_fixtures"]),
            )
        )
    expected_order = sorted(entries, key=lambda item: (item.bot_id, item.build_id))
    if entries != expected_order:
        raise CatalogError("[CATALOG_ORDER_INVALID] entries are not sorted")
    lock_digest = assembly.get("dependency_lock_sha256")
    if not isinstance(lock_digest, str) or not re.fullmatch(
        r"[0-9a-f]{64}", lock_digest
    ):
        raise CatalogError("[ASSEMBLY_INVALID] dependency lock digest is invalid")
    return BotCatalog(
        root, assembly_id, lock_digest, assembly["target_platform"], tuple(entries)
    )


def module_identity(bot_id: str, build_id: str) -> str:
    """Derive a Python-safe, build-unique import identity; never accept it from YAML."""
    if not BOT_ID.fullmatch(bot_id) or not BUILD_ID.fullmatch(build_id):
        raise CatalogError("[BOT_ID_INVALID] cannot derive module identity")
    return f"scrapy_collector.spiders.generated.b_{bot_id.removeprefix('bot-')}__v_{build_id}"
