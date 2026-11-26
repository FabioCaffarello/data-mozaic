"""Deterministic first live Scrapy backend, delegated through the gateway."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from mozaic_bot_build import derive_bot_id

from .backend_python_http import _platform_slug, _safe_output_path
from .backend_python_http import verify_build as _verify_build
from .playbook_v2 import IR_VERSION, IR_VERSION_V2, TypedIR, canonical_json
from .reference_runtime import RUNTIME_VERSION
from .runtime_identity import SCRAPY_RUNTIME_MODULES, runtime_bundle_files
from .scrapy_live_runtime import LIVE_RUNTIME_VERSION
from .syntax import PlaybookError

BACKEND_ID = "scrapy-live-http"
BACKEND_VERSION = "1.0.0"
COMPILER_VERSION = "mozaic-collector-engineering/0.3.0"
PROFILE = json.loads(
    files("collector_engineering")
    .joinpath("profiles/scrapy-live-http-v1.json")
    .read_text(encoding="utf-8")
)


def _supported_graph(
    ir: TypedIR,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    nodes = ir.document["nodes"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_type.setdefault(node["operator_type"], []).append(node)
    if (
        len(by_type.get("http.fetch", [])) != 1
        or len(by_type.get("select.json_records", []))
        + len(by_type.get("select.html_records", []))
        != 1
        or len(by_type.get("extract.fields", [])) != 1
        or len(by_type.get("output.records", [])) != 1
        or len(by_type.get("output.raw", [])) > 1
        or len(by_type.get("links.discover", [])) not in {0, 1}
        or len(by_type.get("pagination.bounded", [])) not in {0, 1}
        or len(by_type.get("links.discover", []))
        != len(by_type.get("pagination.bounded", []))
        or len(nodes) not in {4, 5, 6, 7}
    ):
        raise PlaybookError(
            "[LIVE_GRAPH_UNSUPPORTED] live v1 requires one GET, JSON or HTML selection, extraction and records output"
        )
    fetch = by_type["http.fetch"][0]
    input_query = fetch["inputs"].get("input_query")
    if input_query is not None:
        if (
            not isinstance(input_query, dict)
            or set(input_query) != {"parameter", "query_name"}
            or not all(
                isinstance(value, str) and value for value in input_query.values()
            )
            or ir.document.get("ir_version") != IR_VERSION_V2
        ):
            raise PlaybookError(
                "[LIVE_INPUT_QUERY_INVALID] input query binding is invalid"
            )
        try:
            query_items = parse_qsl(
                urlsplit(fetch["inputs"]["url"]).query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError:
            query_items = []
        if len(query_items) != 1 or query_items[0][0] != input_query["query_name"]:
            raise PlaybookError(
                "[LIVE_INPUT_QUERY_INVALID] URL must contain only the bound query key"
            )
        # M5 parameter binding is deliberately a one-response profile. A
        # redirect or pagination link is another network target and cannot
        # inherit the per-submission query binding safely.
        if by_type.get("links.discover") or by_type.get("pagination.bounded"):
            raise PlaybookError(
                "[LIVE_INPUT_QUERY_PROFILE_UNSUPPORTED] parameterized live requests "
                "must contain exactly one fetch and no continuation operations"
            )
    select = (by_type.get("select.json_records") or by_type.get("select.html_records"))[
        0
    ]
    extract = by_type["extract.fields"][0]
    output = by_type["output.records"][0]
    discover = by_type.get("links.discover", [None])[0]
    pagination = by_type.get("pagination.bounded", [None])[0]
    selector_input = select["inputs"].get(
        "document", select["inputs"].get("documents", {})
    )
    selector_upstream = pagination or fetch
    if (
        selector_input.get("operation") != selector_upstream["operation_id"]
        or extract["inputs"].get("records", {}).get("operation")
        != select["operation_id"]
        or output["inputs"].get("records", {}).get("operation")
        != extract["operation_id"]
        or (
            by_type.get("output.raw")
            and by_type["output.raw"][0]["inputs"].get("documents", {}).get("operation")
            != selector_upstream["operation_id"]
        )
        or (
            discover is not None
            and discover["inputs"].get("document", {}).get("operation")
            != fetch["operation_id"]
        )
        or (
            pagination is not None
            and pagination["inputs"].get("links", {}).get("operation")
            != discover["operation_id"]
        )
    ):
        raise PlaybookError("[LIVE_GRAPH_UNSUPPORTED] live v1 requires a linear graph")
    if pagination is not None:
        page_limit = pagination["inputs"].get("max_pages", 0)
        if (
            select["operator_type"] != "select.html_records"
            or not pagination["inputs"].get("stop_when_no_links")
            or page_limit < 1
            or page_limit > ir.document["budgets"].get("max_pages", 0)
            or page_limit > ir.document["budgets"].get("max_requests", 0)
        ):
            raise PlaybookError(
                "[LIVE_PAGINATION_INVALID] live pagination requires HTML, a planned stop condition and page/request budgets"
            )
    content_type = fetch["inputs"].get("content_type")
    selected_type = select["operator_type"]
    if (
        selected_type == "select.json_records"
        and content_type not in {"application/json", "application/*+json"}
    ) or (selected_type == "select.html_records" and content_type != "text/html"):
        raise PlaybookError(
            "[LIVE_MEDIA_TYPE_UNSUPPORTED] selector and fetch media type differ"
        )
    if ir.document["budgets"].get("max_requests", 0) < 1:
        raise PlaybookError("[LIVE_BUDGET_INVALID] max_requests must be positive")
    return fetch, select, extract, discover, pagination


def generate_bot(ir: TypedIR, runtime_identity: dict[str, Any]) -> bytes:
    """Generate a small executable entrypoint and immutable live plan."""
    fetch, select, extract, discover, pagination = _supported_graph(ir)
    plan = {
        "profile": "scrapy-live-http-v1",
        "source": ir.document["source"],
        "budgets": ir.document["budgets"],
        "fetch": fetch,
        "select": select,
        "extract": extract,
        "discover": discover,
        "pagination": pagination,
    }
    lines = [
        '"""Generated BC15 live entrypoint; all source HTTP remains gateway delegated."""',
        "import sys",
        "from pathlib import Path",
        "sys.path.insert(0, str(Path(__file__).resolve().parent / 'runtime'))",
        "from mozaic_bot_runtime import scrapy_framework",
        f"RUNTIME_IDENTITY = {runtime_identity!r}",
        f"PLAN = {plan!r}",
        "def main(argv=None):",
        "    return scrapy_framework.run_live(PLAN, RUNTIME_IDENTITY, argv)",
        "if __name__ == '__main__':",
        "    raise SystemExit(main())",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def compile_build(
    ir: TypedIR,
    *,
    output: Path,
    lockfile: Path,
    playbook_sha256: str | None = None,
    target_platform: str | None = None,
) -> dict[str, Any]:
    if ir.document.get("ir_version") not in {IR_VERSION, IR_VERSION_V2}:
        raise PlaybookError(
            "[IR_VERSION_UNSUPPORTED] live backend requires collector-ir/v1"
        )
    fetch, _, _, _, _ = _supported_graph(ir)
    if "http.fetch.fixture" not in ir.document["capabilities"]:
        raise PlaybookError(
            "[LIVE_PLAYBOOK_UNSUPPORTED] source graph must be authored from the current fetch operator"
        )
    target_platform = target_platform or _platform_slug()
    if "/" not in target_platform:
        raise PlaybookError("[TARGET_PLATFORM_INVALID] target platform is invalid")
    runtime_files, runtime_identity = runtime_bundle_files(SCRAPY_RUNTIME_MODULES)
    files_map = {
        "bot.py": generate_bot(ir, runtime_identity),
        "capability-profile.json": canonical_json(PROFILE),
        "README.md": b"# Generated Scrapy live HTTP bot\n\nThis build delegates every source request to the authenticated acquisition gateway. Admission and policy authorization are required at runtime.\n",
    }
    files_map.update(runtime_files)
    generated = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(files_map.items())
    }
    input_query = fetch["inputs"].get("input_query")
    live_request = {"method": "GET", "url": fetch["inputs"]["url"]}
    if input_query is not None:
        parsed_url = urlsplit(fetch["inputs"]["url"])
        base_url = urlunsplit(
            (parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", "")
        )
        live_request = {
            "method": "GET",
            "url": base_url,
            "input_query": dict(input_query),
        }
    manifest_core = {
        "manifest_version": (
            "bot-build-manifest/v3"
            if input_query is not None
            else "bot-build-manifest/v2"
        ),
        "playbook_id": ir.document["playbook_id"],
        "bot_id": derive_bot_id(
            ir.document["playbook_id"], ir.document["source"]["source_id"]
        ),
        "source": {
            "source_id": ir.document["source"]["source_id"],
            "source_revision": ir.document["source"]["revision"],
        },
        "playbook_version": (
            "capture-playbook-draft/v3"
            if ir.document["ir_version"] == IR_VERSION_V2
            else "capture-playbook-draft/v2"
        ),
        "playbook_sha256": playbook_sha256,
        "ir_version": ir.document["ir_version"],
        "ir_sha256": ir.sha256,
        "backend_id": BACKEND_ID,
        "backend_version": BACKEND_VERSION,
        "capability_profile_sha256": hashlib.sha256(
            canonical_json(PROFILE)
        ).hexdigest(),
        "capability_profile_id": "scrapy-live-http-v1",
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
            "execution_mode": "live-authorized",
            "runtime_profile": "scrapy-live-http-v1",
            "python_minor": f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}",
            "target_platform": target_platform,
            "scrapy": importlib.metadata.version("scrapy"),
            "scrapy_runtime": LIVE_RUNTIME_VERSION,
        },
        "toolchain": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform_system": platform.system(),
            "platform_machine": platform.machine(),
            "runtime": LIVE_RUNTIME_VERSION,
            "reference_runtime": RUNTIME_VERSION,
            "scrapy": importlib.metadata.version("scrapy"),
            "selectolax": "0.4.13",
        },
        "dependency_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        "generated_files": generated,
        "input_fixtures": [],
        "live_request": live_request,
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
    version = importlib.metadata.version("scrapy")
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
            "scrapy": version,
            "selectolax": "0.4.13",
        },
    )
    compatibility = manifest.get("compatibility", {})
    if (
        manifest.get("capability_profile_id") != "scrapy-live-http-v1"
        or compatibility.get("execution_mode") != "live-authorized"
        or compatibility.get("runtime_profile") != "scrapy-live-http-v1"
        or manifest.get("input_fixtures") != []
    ):
        raise PlaybookError(
            "[BUILD_EXECUTION_MODE_UNSUPPORTED] live build binding differs"
        )
    if manifest.get("toolchain", {}).get("runtime") not in {
        "scrapy-live-http-runtime/1.0.0",
        LIVE_RUNTIME_VERSION,
    }:
        raise PlaybookError(
            "[BUILD_TOOLCHAIN_UNSUPPORTED] Scrapy runtime is unsupported"
        )
    return manifest
