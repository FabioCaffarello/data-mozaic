"""Fresh-process entry point that verifies before importing generated code."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from mozaic_bot_build import BotBuildVerificationError, verify_bot_build

from scrapy_collector.catalog.model import CatalogError, load_catalog


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scrapy-collector-child")
    parser.add_argument("--catalog-root", required=True, type=Path)
    parser.add_argument("--bot-id", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--stage-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        catalog = load_catalog(args.catalog_root)
        expected_assembly_id = os.environ.get("SCRAPY_COLLECTOR_ASSEMBLY_ID")
        if expected_assembly_id and catalog.assembly_id != expected_assembly_id:
            raise CatalogError(
                "[ASSEMBLY_IMAGE_ID_MISMATCH] child catalog differs from parent"
            )
        entry = catalog.resolve(args.bot_id, args.build_id)
        build_root = catalog.build_root(entry)
        verified = verify_bot_build(
            build_root,
            entry.build_manifest_digest,
            supported_versions=("bot-build-manifest/v2",),
        )
        if verified.manifest.get("build_id") != entry.build_id:
            raise CatalogError("[BUILD_ID_MISMATCH] selected BotBuild changed")
        if "mozaic_bot_runtime" in sys.modules:
            raise CatalogError(
                "[RUNTIME_PRELOADED] a BotBuild runtime was imported before selection"
            )
        module_path = build_root / "bot.py"
        if module_path.is_symlink() or not module_path.is_file():
            raise CatalogError("[SPIDER_MODULE_MISSING] generated Spider is missing")
        spec = importlib.util.spec_from_file_location(
            entry.module_identity, module_path
        )
        if spec is None or spec.loader is None:
            raise CatalogError(
                "[SPIDER_MODULE_INVALID] selected Spider cannot be loaded"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[entry.module_identity] = module
        spec.loader.exec_module(module)
        if "mozaic_bot_runtime" not in sys.modules:
            raise CatalogError(
                "[RUNTIME_IMPORT_MISSING] BotBuild runtime was not loaded"
            )
        runtime_root = (build_root / "runtime" / "mozaic_bot_runtime").resolve(
            strict=True
        )
        runtime_names = ["reference_runtime", "scrapy_fixture_runtime"]
        framework_runtime = "scrapy_framework.py" in entry.runtime_identity.get(
            "files", {}
        )
        if framework_runtime:
            runtime_names.append("scrapy_framework")
        for runtime_name in runtime_names:
            loaded = sys.modules.get(f"mozaic_bot_runtime.{runtime_name}")
            if loaded is None or Path(loaded.__file__).resolve().parent != runtime_root:
                raise CatalogError(
                    "[RUNTIME_IMPORT_MISMATCH] runtime came from outside selected BotBuild"
                )
        spider_type = getattr(module, "GeneratedSpider", None)
        if not callable(getattr(module, "main", None)) or (
            not framework_runtime and spider_type is None
        ):
            raise CatalogError(
                "[SPIDER_ENTRYPOINT_INVALID] BotBuild entrypoint is incomplete"
            )
        exit_code = module.main(
            [
                "--fixtures",
                str(catalog.fixture_root(entry)),
                "--output",
                str(args.stage_root / "result"),
            ]
        )
        return int(exit_code)
    except (CatalogError, BotBuildVerificationError, OSError, ValueError) as error:
        print(json.dumps({"state": "failed", "diagnostic": str(error)}, sort_keys=True))
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
