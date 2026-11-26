from __future__ import annotations

import ast
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from collector_engineering.backend_scrapy_live import compile_build, verify_build
from collector_engineering.playbook_v2 import (
    PlaybookError,
    build_typed_ir,
    load_playbook_v2,
)
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[3]


class ScrapyLiveBackendTests(unittest.TestCase):
    def test_compiles_verifiable_live_build_without_fixture_binding(self):
        example = ROOT / "examples/playbooks/json-api"
        parsed = load_playbook_v2(example / "capture-playbook.yaml")
        ir = build_typed_ir(parsed, fixture_root=example / "fixtures")
        with tempfile.TemporaryDirectory(
            prefix=".dm-live-build-test-", dir=ROOT
        ) as temporary:
            build = Path(temporary) / "bot-build"
            manifest = compile_build(
                ir,
                output=build,
                lockfile=ROOT / "uv.lock",
                target_platform="linux/amd64",
            )
            verified = verify_build(build)
            self.assertEqual(manifest["build_id"], verified["build_id"])
            self.assertEqual(
                "live-authorized", verified["compatibility"]["execution_mode"]
            )
            self.assertEqual(
                "scrapy-live-http-v1", verified["compatibility"]["runtime_profile"]
            )
            self.assertEqual([], verified["input_fixtures"])
            self.assertEqual("GET", verified["live_request"]["method"])
            ast.parse((build / "bot.py").read_text(encoding="utf-8"))

    def test_compiles_live_html_selection_with_verified_delegated_runtime(self):
        example = ROOT / "examples/playbooks/html-simple"
        parsed = load_playbook_v2(example / "capture-playbook.yaml")
        ir = build_typed_ir(parsed, fixture_root=example / "fixtures")
        with tempfile.TemporaryDirectory(
            prefix=".dm-live-html-build-test-", dir=ROOT
        ) as temporary:
            build = Path(temporary) / "bot-build"
            compile_build(
                ir,
                output=build,
                lockfile=ROOT / "uv.lock",
                target_platform="linux/amd64",
            )
            verified = verify_build(build)
            generated = (build / "bot.py").read_text(encoding="utf-8")
            self.assertEqual("scrapy-live-http", verified["backend_id"])
            self.assertIn("scrapy_framework.run_live", generated)
            self.assertIn("select.html_records", generated)
            self.assertNotIn("CrawlerProcess", generated)
            ast.parse(generated)

    def test_compiles_bounded_live_next_url_pagination(self):
        example = ROOT / "examples/playbooks/multipage-catalog"
        parsed = load_playbook_v2(example / "capture-playbook.yaml")
        ir = build_typed_ir(parsed, fixture_root=example / "fixtures")
        with tempfile.TemporaryDirectory(
            prefix=".dm-live-pagination-build-test-", dir=ROOT
        ) as temporary:
            build = Path(temporary) / "bot-build"
            compile_build(
                ir,
                output=build,
                lockfile=ROOT / "uv.lock",
                target_platform="linux/amd64",
            )
            generated = (build / "bot.py").read_text(encoding="utf-8")
            self.assertIn("scrapy_framework.run_live", generated)
            self.assertIn("'pagination':", generated)
            self.assertNotIn("PAGINATION_LOOP", generated)
            self.assertNotIn("CrawlerProcess", generated)
            ast.parse(generated)

    def test_parameterized_live_build_is_v3_and_deterministic(self):
        example = ROOT / "examples/playbooks/json-api"
        parsed = load_playbook_v2(example / "capture-playbook.yaml")
        document = copy.deepcopy(parsed.document)
        document["dsl_version"] = "capture-playbook-draft/v3"
        document["source"]["path_prefixes"] = ["/v1"]
        fetch = next(
            operation
            for operation in document["operations"]
            if operation["op"] == "http.fetch/v1"
        )
        fetch["url"] = "https://api.fixture.test/v1/items?limit=1"
        fetch["input_query"] = {"parameter": "limit", "query_name": "limit"}
        parameterized = type(parsed)(document)
        with tempfile.TemporaryDirectory(
            prefix=".dm-live-parameterized-build-test-", dir=ROOT
        ) as temporary:
            root = Path(temporary)
            fixture_root = root / "fixtures"
            shutil.copytree(example / "fixtures", fixture_root)
            index_path = fixture_root / "index.json"
            fixture_index = json.loads(index_path.read_text())
            fixture_index["pages"][0]["url"] = fetch["url"]
            index_path.write_text(json.dumps(fixture_index))
            ir = build_typed_ir(parameterized, fixture_root=fixture_root)
            first = root / "first"
            second = root / "second"
            first_manifest = compile_build(
                ir,
                output=first,
                lockfile=ROOT / "uv.lock",
                target_platform="linux/amd64",
            )
            second_manifest = compile_build(
                ir,
                output=second,
                lockfile=ROOT / "uv.lock",
                target_platform="linux/amd64",
            )
            self.assertEqual(
                "bot-build-manifest/v3", first_manifest["manifest_version"]
            )
            self.assertEqual(first_manifest["build_id"], second_manifest["build_id"])
            self.assertEqual(
                first_manifest["runtime_identity"],
                second_manifest["runtime_identity"],
            )
            self.assertEqual(
                first_manifest["live_request"],
                {
                    "method": "GET",
                    "url": "https://api.fixture.test/v1/items",
                    "input_query": {
                        "parameter": "limit",
                        "query_name": "limit",
                    },
                },
            )
            self.assertEqual(first_manifest, verify_build(second))
            schema = json.loads(
                (
                    ROOT / "contracts/bot-build/draft/v3/BotBuildManifest.schema.json"
                ).read_text()
            )
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(
                first_manifest
            )

    def test_parameterized_live_profile_rejects_pagination(self):
        example = ROOT / "examples/playbooks/multipage-catalog"
        parsed = load_playbook_v2(example / "capture-playbook.yaml")
        document = copy.deepcopy(parsed.document)
        document["dsl_version"] = "capture-playbook-draft/v3"
        document["source"]["path_prefixes"] = ["/catalog"]
        fetch = next(
            operation
            for operation in document["operations"]
            if operation["op"] == "http.fetch/v1"
        )
        fetch["url"] = "https://pages.fixture.test/catalog/page-1?category=A"
        fetch["input_query"] = {
            "parameter": "category",
            "query_name": "category",
        }
        parameterized = type(parsed)(document)
        with tempfile.TemporaryDirectory(
            prefix=".dm-live-parameterized-pagination-test-", dir=ROOT
        ) as temporary:
            root = Path(temporary)
            fixture_root = root / "fixtures"
            shutil.copytree(example / "fixtures", fixture_root)
            index_path = fixture_root / "index.json"
            fixture_index = json.loads(index_path.read_text())
            fixture_index["pages"][0]["url"] = fetch["url"]
            index_path.write_text(json.dumps(fixture_index))
            ir = build_typed_ir(parameterized, fixture_root=fixture_root)
            with self.assertRaisesRegex(PlaybookError, "one fetch"):
                compile_build(
                    ir,
                    output=root / "rejected-build",
                    lockfile=ROOT / "uv.lock",
                    target_platform="linux/amd64",
                )

    def test_raw_artifact_v4_compiles_without_record_contract_or_parser(self):
        example = ROOT / "examples/playbooks/json-api"
        parsed = load_playbook_v2(example / "capture-playbook.yaml")
        document = copy.deepcopy(parsed.document)
        document["dsl_version"] = "capture-playbook-draft/v4"
        document["capabilities"] = ["http.fetch.fixture", "output.artifact.reference"]
        document["output_contract"] = {
            "mode": "artifact",
            "media_types": ["application/pdf"],
            "max_bytes": 512,
        }
        fetch = next(op for op in document["operations"] if op["op"] == "http.fetch/v1")
        fetch["url"] = "https://api.fixture.test/v1/report"
        fetch["content_type"] = "application/pdf"
        document["operations"] = [
            fetch,
            {
                "id": "artifact",
                "op": "output.artifact/v1",
                "document": {"operation": fetch["id"], "port": "document"},
            },
        ]
        temporary = tempfile.TemporaryDirectory(
            prefix=".dm-raw-artifact-test-", dir=ROOT
        )
        self.addCleanup(temporary.cleanup)
        fixture_root = Path(temporary.name) / "fixtures"
        shutil.copytree(example / "fixtures", fixture_root)
        fixture_index = json.loads((fixture_root / "index.json").read_text())
        fixture_index["pages"][0]["url"] = fetch["url"]
        fixture_index["pages"][0]["content_type"] = "application/pdf"
        (fixture_root / "index.json").write_text(json.dumps(fixture_index))
        ir = build_typed_ir(type(parsed)(document), fixture_root=fixture_root)
        build = Path(temporary.name) / "build"
        manifest = compile_build(
            ir,
            output=build,
            lockfile=ROOT / "uv.lock",
            target_platform="linux/amd64",
        )
        self.assertEqual("collector-ir/v3", ir.document["ir_version"])
        self.assertEqual("bot-build-manifest/v4", manifest["manifest_version"])
        self.assertEqual("capture-playbook-draft/v4", manifest["playbook_version"])
        self.assertEqual(
            {"method": "GET", "url": fetch["url"]}, manifest["live_request"]
        )
        generated = (build / "bot.py").read_text(encoding="utf-8")
        self.assertNotIn("extract.fields", generated)
        self.assertEqual(manifest, verify_build(build))
        schema = json.loads(
            (
                ROOT / "contracts/bot-build/draft/v4/BotBuildManifest.schema.json"
            ).read_text()
        )
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(manifest)


if __name__ == "__main__":
    unittest.main()
