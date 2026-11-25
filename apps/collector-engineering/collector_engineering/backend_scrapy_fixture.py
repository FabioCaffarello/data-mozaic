"""Deterministic compiler for the Scrapy fixture-only backend."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any

from mozaic_bot_build import derive_bot_id

from .backend_python_http import _platform_slug, _safe_output_path
from .backend_python_http import verify_build as _verify_build
from .fixture_evidence import inspect_fixture_evidence
from .playbook_v2 import IR_VERSION, TypedIR, canonical_json
from .reference_runtime import RUNTIME_VERSION
from .runtime_identity import SCRAPY_RUNTIME_MODULES, runtime_bundle_files
from .scrapy_fixture_runtime import SCRAPY_RUNTIME_VERSION
from .syntax import PlaybookError

BACKEND_ID = "scrapy-fixture"
BACKEND_VERSION = "1.0.0"
COMPILER_VERSION = "mozaic-collector-engineering/0.3.0"
PROFILE = json.loads(
    files("collector_engineering")
    .joinpath("profiles/scrapy-fixture-v1.json")
    .read_text(encoding="utf-8")
)
BACKEND_CAPABILITIES = frozenset(PROFILE["capabilities"])


def _callback_name(role: str, operation_id: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_]", "_", operation_id)[:32] or "operation"
    suffix = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:8]
    return f"_op_{role}_{readable}_{suffix}"


def check_capabilities(ir: TypedIR) -> list[dict[str, str]]:
    missing = sorted(set(ir.document["capabilities"]) - BACKEND_CAPABILITIES)
    return [
        {
            "code": "CAPABILITY_UNSUPPORTED",
            "capability": capability,
            "backend": BACKEND_ID,
            "message": f"backend {BACKEND_ID} does not support {capability}",
        }
        for capability in missing
    ]


def _supported_graph(
    ir: TypedIR,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    nodes = ir.document["nodes"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        name = f"{node['operator_type']}/{node['operator_version']}"
        by_type.setdefault(name, []).append(node)
    fetches = by_type.get("http.fetch/v1", [])
    selections = by_type.get("select.html_records/v1", []) + by_type.get(
        "select.json_records/v1", []
    )
    extracts = by_type.get("extract.fields/v1", [])
    record_outputs = by_type.get("output.records/v1", [])
    if (
        len(fetches) != 1
        or len(selections) != 1
        or len(extracts) != 1
        or len(record_outputs) != 1
        or len(by_type.get("browser.render_dom/v1", []))
        or len(by_type.get("select.rendered_dom_records/v1", []))
    ):
        raise PlaybookError(
            "[BACKEND_GRAPH_UNSUPPORTED] Scrapy fixture backend requires one fetch, one selection, one extraction and one record output"
        )
    discover = by_type.get("links.discover/v1", [])
    paginate = by_type.get("pagination.bounded/v1", [])
    if len(discover) > 1 or len(paginate) > 1 or bool(discover) != bool(paginate):
        raise PlaybookError(
            "[BACKEND_GRAPH_UNSUPPORTED] Scrapy fixture backend supports one bounded discovery and pagination pair"
        )
    allowed = {
        "http.fetch/v1",
        "links.discover/v1",
        "pagination.bounded/v1",
        "select.html_records/v1",
        "select.json_records/v1",
        "extract.fields/v1",
        "output.records/v1",
        "output.raw/v1",
    }
    unsupported = sorted(set(by_type) - allowed)
    if unsupported:
        raise PlaybookError(
            f"[BACKEND_GRAPH_UNSUPPORTED] unsupported operation graph: {', '.join(unsupported)}"
        )
    fetch, select, extract = fetches[0], selections[0], extracts[0]
    if discover:
        if (
            discover[0]["inputs"]["document"]["operation"] != fetch["operation_id"]
            or paginate[0]["inputs"]["links"]["operation"]
            != discover[0]["operation_id"]
            or select["inputs"]["documents"]["operation"] != paginate[0]["operation_id"]
        ):
            raise PlaybookError(
                "[BACKEND_GRAPH_UNSUPPORTED] pagination bindings do not match the supported linear graph"
            )
    else:
        selection_binding = select["inputs"].get("document") or select["inputs"].get(
            "documents"
        )
        if selection_binding["operation"] != fetch["operation_id"]:
            raise PlaybookError(
                "[BACKEND_GRAPH_UNSUPPORTED] selection must bind to the single fetch operation"
            )
    if extract["inputs"]["records"]["operation"] != select["operation_id"]:
        raise PlaybookError(
            "[BACKEND_GRAPH_UNSUPPORTED] extraction must bind to the single selection operation"
        )
    if record_outputs[0]["inputs"]["records"]["operation"] != extract["operation_id"]:
        raise PlaybookError(
            "[BACKEND_GRAPH_UNSUPPORTED] output must bind to the single extraction operation"
        )
    return fetch, select, discover[0] if discover else None


def generate_spider(
    ir: TypedIR,
    fixtures: list[dict[str, Any]],
    runtime_identity: dict[str, Any],
) -> bytes:
    """Generate a small executable entrypoint and immutable fixture plan."""
    fetch, select, discover = _supported_graph(ir)
    extract = next(
        node
        for node in ir.document["nodes"]
        if node["operator_type"] == "extract.fields"
    )
    pagination = next(
        (
            node
            for node in ir.document["nodes"]
            if node["operator_type"] == "pagination.bounded"
        ),
        None,
    )
    page_limit = ir.document["budgets"]["max_pages"]
    if pagination is not None:
        page_limit = min(page_limit, pagination["inputs"]["max_pages"])
    if page_limit > 200:
        raise PlaybookError(
            "[BACKEND_BUDGET_UNSUPPORTED] page limit exceeds Scrapy fixture bound"
        )
    plan = {
        "profile": "scrapy-fixture-v1",
        "source": ir.document["source"],
        "budgets": ir.document["budgets"],
        "fixtures": fixtures,
        "fetch": fetch,
        "select": select,
        "extract": extract,
        "discover": discover,
        "page_limit": page_limit,
        "raw_output": any(
            node["operator_type"] == "output.raw" for node in ir.document["nodes"]
        ),
    }
    lines = [
        '"""Generated BC15 fixture entrypoint; the pinned runtime owns Scrapy lifecycle."""',
        "import sys",
        "from pathlib import Path",
        "sys.path.insert(0, str(Path(__file__).resolve().parent / 'runtime'))",
        "from mozaic_bot_runtime import scrapy_framework",
        f"RUNTIME_IDENTITY = {runtime_identity!r}",
        f"PLAN = {plan!r}",
        "def main(argv=None):",
        "    return scrapy_framework.run_fixture(PLAN, RUNTIME_IDENTITY, argv)",
        "if __name__ == '__main__':",
        "    raise SystemExit(main())",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def compile_build(
    ir: TypedIR,
    *,
    fixture_root: Path,
    output: Path,
    lockfile: Path,
    playbook_sha256: str | None = None,
    target_platform: str | None = None,
) -> dict[str, Any]:
    if ir.document.get("ir_version") != IR_VERSION:
        raise PlaybookError(
            "[IR_VERSION_UNSUPPORTED] Scrapy backend requires collector-ir/v1"
        )
    capability_failures = check_capabilities(ir)
    if capability_failures:
        summary = "; ".join(
            f"{item['code']} {item['capability']}" for item in capability_failures
        )
        raise PlaybookError(summary)
    _supported_graph(ir)
    page_map, fixture_diagnostics = inspect_fixture_evidence(fixture_root)
    if fixture_diagnostics:
        raise PlaybookError(
            "; ".join(
                f"{item.code} {item.path}: {item.message}"
                for item in fixture_diagnostics
            )
        )
    evidence_refs = {item["evidence_ref"] for item in ir.document["evidence"]}
    fixtures = [
        {
            "url": page["url"],
            "evidence_ref": page["evidence_ref"],
            "sha256": page["sha256"],
            "size_bytes": page["size_bytes"],
            "content_type": page["content_type"],
            "status": page["status"],
            "discovered_from": page.get("discovered_from"),
            "source_id": page["source_id"],
            "source_revision": page["source_revision"],
        }
        for page in page_map.values()
        if page.get("evidence_ref") in evidence_refs
    ]
    fixtures.sort(key=lambda item: (item["url"], item["evidence_ref"]))
    if not fixtures:
        raise PlaybookError(
            "[FIXTURE_EVIDENCE_MISSING] no build fixtures match the IR evidence"
        )
    target_platform = target_platform or _platform_slug()
    if (
        not isinstance(target_platform, str)
        or "/" not in target_platform
        or any(
            not part or not part.replace("-", "").replace("_", "").isalnum()
            for part in target_platform.split("/", 1)
        )
    ):
        raise PlaybookError("[TARGET_PLATFORM_INVALID] target platform is invalid")
    if any(
        item["source_id"] != ir.document["source"]["source_id"]
        or item["source_revision"] != ir.document["source"]["revision"]
        for item in fixtures
    ):
        raise PlaybookError("[SOURCE_REVISION_MISMATCH] fixture source differs from IR")
    runtime_files, runtime_identity = runtime_bundle_files(SCRAPY_RUNTIME_MODULES)
    files_map = {
        "bot.py": generate_spider(ir, fixtures, runtime_identity),
        "capability-profile.json": canonical_json(PROFILE),
        "README.md": (
            b"# Generated Scrapy fixture bot\n\n"
            b"This Scrapy Spider uses an exact-identity fixture-only HTTP/HTTPS download handler. "
            b"It has no network fallback. The engine runtime is pinned by the BotBuild manifest.\n\n"
            b"Run with the locked mozaic-collector-engineering environment:\n\n"
            b"```sh\npython -B bot.py --fixtures PATH --output PATH\n```\n"
        ),
    }
    files_map.update(runtime_files)
    generated = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(files_map.items())
    }
    manifest_core = {
        "manifest_version": "bot-build-manifest/v2",
        "playbook_id": ir.document["playbook_id"],
        "bot_id": derive_bot_id(
            ir.document["playbook_id"], ir.document["source"]["source_id"]
        ),
        "source": {
            "source_id": ir.document["source"]["source_id"],
            "source_revision": ir.document["source"]["revision"],
        },
        "playbook_version": "capture-playbook-draft/v2",
        "playbook_sha256": playbook_sha256,
        "ir_version": IR_VERSION,
        "ir_sha256": ir.sha256,
        "backend_id": BACKEND_ID,
        "backend_version": BACKEND_VERSION,
        "capability_profile_sha256": hashlib.sha256(
            canonical_json(PROFILE)
        ).hexdigest(),
        "capability_profile_id": "scrapy-fixture-v1",
        "compiler_version": COMPILER_VERSION,
        "runtime_identity": runtime_identity,
        "dependency_identity": {
            "lockfile_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
            "packages": {
                "scrapy": importlib.metadata.version("scrapy"),
                "selectolax": "0.4.13",
            },
        },
        "compatibility": {
            "worker_contract": "scrapy-collector/v1",
            "execution_mode": "fixture-only",
            "python_minor": f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}",
            "target_platform": target_platform,
            "scrapy": importlib.metadata.version("scrapy"),
            "scrapy_runtime": SCRAPY_RUNTIME_VERSION,
        },
        "toolchain": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform_system": platform.system(),
            "platform_machine": platform.machine(),
            "runtime": SCRAPY_RUNTIME_VERSION,
            "reference_runtime": RUNTIME_VERSION,
            "scrapy": importlib.metadata.version("scrapy"),
            "selectolax": "0.4.13",
        },
        "dependency_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        "generated_files": generated,
        "input_fixtures": fixtures,
    }
    manifest = {
        **manifest_core,
        "build_id": hashlib.sha256(canonical_json(manifest_core)).hexdigest(),
    }
    destination = _safe_output_path(output)
    destination.mkdir(parents=True, mode=0o700)
    try:
        for name, data in files_map.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.is_symlink() or ".." in Path(name).parts:
                raise PlaybookError("[BUILD_PATH_UNSAFE] generated file path is unsafe")
            with target.open("xb") as stream:
                stream.write(data)
        with (destination / "manifest.json").open("xb") as stream:
            stream.write(
                json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
            )
    except BaseException:
        shutil.rmtree(destination)
        raise
    return manifest


def verify_build(directory: Path) -> dict[str, Any]:
    try:
        scrapy_version = importlib.metadata.version("scrapy")
    except importlib.metadata.PackageNotFoundError as error:
        raise PlaybookError(
            "[BUILD_TOOLCHAIN_UNSUPPORTED] Scrapy is not installed"
        ) from error
    manifest = _verify_build(
        directory,
        expected_backend_id=BACKEND_ID,
        expected_backend_version=BACKEND_VERSION,
        expected_compiler_version=COMPILER_VERSION,
        profile=PROFILE,
        expected_toolchain={
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform_system": platform.system(),
            "platform_machine": platform.machine(),
            "reference_runtime": RUNTIME_VERSION,
            "scrapy": scrapy_version,
            "selectolax": "0.4.13",
        },
    )
    if manifest.get("manifest_version") == "bot-build-manifest/v2":
        if manifest.get("capability_profile_id") != "scrapy-fixture-v1":
            raise PlaybookError("[BUILD_PROFILE_UNSUPPORTED] profile identity differs")
        compatibility = manifest.get("compatibility", {})
        if compatibility.get("worker_contract") != "scrapy-collector/v1":
            raise PlaybookError("[BUILD_WORKER_UNSUPPORTED] worker contract differs")
        if compatibility.get("execution_mode") != "fixture-only":
            raise PlaybookError(
                "[BUILD_EXECUTION_MODE_UNSUPPORTED] build is not fixture-only"
            )
        if manifest.get("toolchain", {}).get("runtime") not in {
            "scrapy-fixture-runtime/1.0.0",
            SCRAPY_RUNTIME_VERSION,
        }:
            raise PlaybookError(
                "[BUILD_TOOLCHAIN_UNSUPPORTED] Scrapy runtime is unsupported"
            )
    return manifest
