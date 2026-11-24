"""CLI for catalog inspection, build-time assembly, and exact-ID execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mozaic_bot_build.verification import canonical_json

from scrapy_collector.catalog.assembly import AssemblyError, assemble_catalog
from scrapy_collector.catalog.model import BotCatalog, CatalogError, load_catalog
from scrapy_collector.execution.context import ExecutionContext
from scrapy_collector.execution.launcher import ExecutionError, run


def _catalog_root() -> Path:
    configured = os.environ.get("SCRAPY_COLLECTOR_CATALOG_ROOT")
    return (
        Path(configured)
        if configured
        else Path(__file__).resolve().parent / "catalog_data"
    )


def _load_installed_catalog() -> BotCatalog:
    catalog = load_catalog(_catalog_root())
    expected = os.environ.get("SCRAPY_COLLECTOR_ASSEMBLY_ID")
    if expected and catalog.assembly_id != expected:
        raise CatalogError(
            "[ASSEMBLY_IMAGE_ID_MISMATCH] catalog differs from image assembly identity"
        )
    return catalog


def _serialize_entry(entry: Any) -> dict[str, Any]:
    return {
        "bot_id": entry.bot_id,
        "build_id": entry.build_id,
        "status": entry.status,
        "playbook_id": entry.playbook_id,
        "source": entry.source,
        "backend_id": entry.backend_id,
        "capability_profile_id": entry.capability_profile_id,
        "module_identity": entry.module_identity,
        "spider_name": entry.spider_name,
        "execution_modes": ["fixture-only"],
    }


def _request_selection(
    catalog: BotCatalog, request_path: Path
) -> tuple[dict[str, Any], str, str, float]:
    if (
        request_path != Path("/run/execution-request.json")
        or request_path.is_symlink()
        or not request_path.is_file()
        or request_path.stat().st_size > 64_000
    ):
        raise CatalogError(
            "[EXECUTION_REQUEST_INVALID] request path is not the protected mount"
        )

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate request key")
            result[key] = value
        return result

    try:
        request = json.loads(
            request_path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise CatalogError(
            "[EXECUTION_REQUEST_INVALID] request is not valid JSON"
        ) from error
    expected = {
        "contract_version",
        "request_id",
        "run_id",
        "attempt_id",
        "execution_id",
        "source",
        "bot",
        "assembly",
        "worker_image",
        "fixtures",
        "execution_mode",
        "deadline",
        "budgets",
        "correlation",
    }
    if (
        not isinstance(request, dict)
        or set(request) != expected
        or request.get("contract_version") != "execution-request/v0"
        or request.get("execution_mode") != "fixture-only"
    ):
        raise CatalogError("[EXECUTION_REQUEST_INVALID] request schema is closed")
    for name, keys in (
        ("source", {"source_id", "source_revision"}),
        ("bot", {"bot_id", "build_id", "manifest_digest"}),
        ("assembly", {"assembly_id", "catalog_digest"}),
        ("worker_image", {"oci_image", "oci_digest", "local_image_id"}),
        ("fixtures", {"bundle_id", "bundle_digest"}),
        ("budgets", {"duration_seconds", "output_bytes", "records"}),
    ):
        if not isinstance(request[name], dict) or set(request[name]) != keys:
            raise CatalogError(
                "[EXECUTION_REQUEST_INVALID] nested request schema is closed"
            )
    if not all(
        isinstance(request[key], str) and request[key]
        for key in ("request_id", "run_id", "attempt_id", "execution_id", "deadline")
    ):
        raise CatalogError(
            "[EXECUTION_REQUEST_INVALID] request identities are required"
        )
    if len(request["request_id"]) > 128:
        raise CatalogError("[EXECUTION_REQUEST_INVALID] request identity is too long")
    try:
        for key in ("run_id", "attempt_id", "execution_id"):
            uuid.UUID(request[key])
    except (ValueError, AttributeError) as error:
        raise CatalogError(
            "[EXECUTION_REQUEST_INVALID] UUID identity is invalid"
        ) from error
    if (
        not isinstance(request["correlation"], dict)
        or len(request["correlation"]) > 8
        or any(
            key not in {"trace_id", "correlation_id", "intake_id"}
            or not isinstance(value, str)
            or len(value) > 128
            for key, value in request["correlation"].items()
        )
    ):
        raise CatalogError(
            "[EXECUTION_REQUEST_INVALID] correlation metadata exceeds allowlist"
        )
    worker_image = request["worker_image"]
    if any(
        not isinstance(worker_image[key], str) or not worker_image[key]
        for key in worker_image
    ):
        raise CatalogError("[EXECUTION_REQUEST_INVALID] image identities are invalid")
    if (
        not worker_image["oci_image"].endswith("@" + worker_image["oci_digest"])
        or not worker_image["oci_digest"].startswith("sha256:")
        or not worker_image["local_image_id"].startswith("sha256:")
    ):
        raise CatalogError(
            "[EXECUTION_REQUEST_INVALID] image identities are inconsistent"
        )
    bot = request["bot"]
    entry = catalog.resolve(bot["bot_id"], bot["build_id"])
    manifest_bytes = (catalog.build_root(entry) / "manifest.json").read_bytes()
    catalog_digest = hashlib.sha256(
        (catalog.root / "catalog.json").read_bytes()
    ).hexdigest()
    fixture_digest = hashlib.sha256(
        canonical_json(list(entry.input_fixtures))
    ).hexdigest()
    checks = (
        bot["manifest_digest"] == hashlib.sha256(manifest_bytes).hexdigest(),
        request["source"] == entry.source,
        request["assembly"]
        == {"assembly_id": catalog.assembly_id, "catalog_digest": catalog_digest},
        request["fixtures"]
        == {"bundle_id": entry.build_id, "bundle_digest": fixture_digest},
    )
    budgets = request["budgets"]
    if (
        not all(checks)
        or not isinstance(budgets["duration_seconds"], int)
        or isinstance(budgets["duration_seconds"], bool)
        or not 1 <= budgets["duration_seconds"] <= 300
        or not isinstance(budgets["output_bytes"], int)
        or not 1 <= budgets["output_bytes"] <= 16_777_216
        or not isinstance(budgets["records"], int)
        or not 1 <= budgets["records"] <= 10_000
    ):
        raise CatalogError(
            "[EXECUTION_REQUEST_IDENTITY_MISMATCH] request differs from installed immutable catalog"
        )
    try:
        deadline = datetime.fromisoformat(request["deadline"])
    except (AttributeError, ValueError) as error:
        raise CatalogError("[EXECUTION_REQUEST_INVALID] deadline is invalid") from error
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise CatalogError("[EXECUTION_REQUEST_INVALID] deadline needs a timezone")
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise CatalogError("[EXECUTION_REQUEST_EXPIRED] deadline elapsed")
    return (
        request,
        entry.bot_id,
        entry.build_id,
        min(float(budgets["duration_seconds"]), remaining),
    )


def _run_command(catalog: BotCatalog, args: argparse.Namespace) -> int:
    output_root = os.environ.get("SCRAPY_COLLECTOR_OUTPUT_ROOT")
    if not output_root:
        print(
            "[OUTPUT_ROOT_UNCONFIGURED] SCRAPY_COLLECTOR_OUTPUT_ROOT is required",
            file=sys.stderr,
        )
        return 2
    request: dict[str, Any] | None = None
    try:
        if args.request_file is not None:
            request, bot_id, build_id, timeout_seconds = _request_selection(
                catalog, args.request_file
            )
            if args.bot_id is not None or args.build_id is not None:
                raise CatalogError("[EXECUTION_REQUEST_INVALID] mixed selector modes")
        else:
            if not args.bot_id or not args.build_id:
                raise CatalogError(
                    "[BOT_BUILD_REQUIRED] exact bot_id and build_id are required"
                )
            bot_id, build_id, timeout_seconds = (
                args.bot_id,
                args.build_id,
                args.timeout_seconds,
            )
        result = run(
            catalog,
            bot_id,
            build_id,
            ExecutionContext(Path(output_root), timeout_seconds=timeout_seconds),
        )
    except (CatalogError, ExecutionError, OSError, ValueError) as error:
        print(json.dumps({"state": "failed", "diagnostic": str(error)}, sort_keys=True))
        return 1
    if request is None:
        print(json.dumps(result, sort_keys=True))
        return (
            0
            if result["state"] == "succeeded"
            else 124
            if result["state"] == "timed_out"
            else 130
            if result["state"] == "cancelled"
            else 1
        )

    technical_status = result["state"]
    records_count = 0
    output_bytes = 0
    records_sha256 = None
    temporary_ref = None
    if technical_status == "succeeded":
        result_dir = Path(output_root) / result["execution_id"]
        records_path = result_dir / "records.jsonl"
        records = records_path.read_bytes()
        report = result.get("report", {})
        if (
            len(records) > request["budgets"]["output_bytes"]
            or report.get("records", 0) > request["budgets"]["records"]
        ):
            import shutil

            shutil.rmtree(result_dir, ignore_errors=True)
            technical_status = "failed"
            diagnostic = "[EXECUTION_OUTPUT_LIMIT_EXCEEDED] request budget exceeded"
        else:
            records_count = report["records"]
            output_bytes = sum(
                path.stat().st_size for path in result_dir.iterdir() if path.is_file()
            )
            records_sha256 = hashlib.sha256(records).hexdigest()
            temporary_ref = result["execution_id"]
            diagnostic = None
    else:
        diagnostic = result.get("diagnostic", f"worker ended as {technical_status}")
    diagnostics = []
    if diagnostic:
        code = "WORKER_FAILED"
        if diagnostic.startswith("[") and "]" in diagnostic:
            code = diagnostic[1 : diagnostic.index("]")][:64]
        diagnostics.append({"code": code, "message": str(diagnostic)[:1000]})
    worker_image = request["worker_image"]
    report_doc = {
        "contract_version": "execution-report/v0",
        "request_id": request["request_id"],
        "run_id": request["run_id"],
        "attempt_id": request["attempt_id"],
        "execution_id": request["execution_id"],
        "identities": {
            "source_id": request["source"]["source_id"],
            "source_revision": request["source"]["source_revision"],
            "bot_id": bot_id,
            "build_id": build_id,
            "bot_build_manifest_digest": request["bot"]["manifest_digest"],
            "assembly_id": request["assembly"]["assembly_id"],
            "catalog_digest": request["assembly"]["catalog_digest"],
            "oci_digest": worker_image["oci_digest"],
            "local_image_id": worker_image["local_image_id"],
            "fixture_bundle_id": request["fixtures"]["bundle_id"],
            "fixture_bundle_digest": request["fixtures"]["bundle_digest"],
        },
        "technical_status": technical_status,
        "duration_ms": max(0, int(float(result.get("duration_seconds", 0)) * 1000)),
        "counts": {"records": records_count, "output_bytes": output_bytes},
        "diagnostics": diagnostics,
        "outputs": {"records_sha256": records_sha256, "temporary_ref": temporary_ref},
    }
    report_path = Path(output_root) / "execution-report.json"
    payload = json.dumps(report_doc, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > 64_000:
        return 70
    try:
        with report_path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        return 70
    return (
        0
        if technical_status == "succeeded"
        else 124
        if technical_status == "timed_out"
        else 130
        if technical_status == "cancelled"
        else 1
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scrapy-collector")
    subparsers = parser.add_subparsers(dest="command", required=True)
    catalog_parser = subparsers.add_parser(
        "catalog", help="inspect installed catalog metadata"
    )
    catalog_parser.add_subparsers(dest="catalog_command", required=True).add_parser(
        "list"
    )
    run_parser = subparsers.add_parser("run", help="execute one exact BotBuild")
    run_parser.add_argument("--bot-id")
    run_parser.add_argument("--build-id")
    run_parser.add_argument("--request-file", type=Path)
    run_parser.add_argument("--timeout-seconds", type=float, default=300.0)
    assemble_parser = subparsers.add_parser(
        "assemble", help="assemble immutable builds during image construction"
    )
    assemble_parser.add_argument("--manifest", type=Path, required=True)
    assemble_parser.add_argument("--build-root", type=Path, required=True)
    assemble_parser.add_argument("--fixture-root", type=Path, required=True)
    assemble_parser.add_argument("--output", type=Path, required=True)
    assemble_parser.add_argument("--lockfile", type=Path, required=True)
    assemble_parser.add_argument("--target-platform", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "assemble":
            result = assemble_catalog(
                manifest_path=args.manifest,
                build_root=args.build_root,
                fixture_root=args.fixture_root,
                output=args.output,
                lockfile=args.lockfile,
                target_platform=args.target_platform,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        catalog = _load_installed_catalog()
        if args.command == "catalog":
            print(
                json.dumps(
                    {
                        "assembly_id": catalog.assembly_id,
                        "target_platform": catalog.target_platform,
                        "entries": [_serialize_entry(item) for item in catalog.entries],
                    },
                    sort_keys=True,
                )
            )
            return 0
        return _run_command(catalog, args)
    except (AssemblyError, CatalogError, OSError, ValueError) as error:
        print(json.dumps({"state": "failed", "diagnostic": str(error)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
