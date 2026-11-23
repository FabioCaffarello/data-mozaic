"""CLI for catalog inspection, build-time assembly, and exact-ID execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

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


def _run_command(catalog: BotCatalog, args: argparse.Namespace) -> int:
    output_root = os.environ.get("SCRAPY_COLLECTOR_OUTPUT_ROOT")
    if not output_root:
        print(
            "[OUTPUT_ROOT_UNCONFIGURED] SCRAPY_COLLECTOR_OUTPUT_ROOT is required",
            file=sys.stderr,
        )
        return 2
    try:
        result = run(
            catalog,
            args.bot_id,
            args.build_id,
            ExecutionContext(Path(output_root), timeout_seconds=args.timeout_seconds),
        )
    except (CatalogError, ExecutionError, OSError, ValueError) as error:
        print(json.dumps({"state": "failed", "diagnostic": str(error)}, sort_keys=True))
        return 1
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
    run_parser.add_argument("--bot-id", required=True)
    run_parser.add_argument("--build-id", required=True)
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
