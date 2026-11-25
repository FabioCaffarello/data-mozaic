from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from collector_engineering.backend_scrapy_live import compile_build, verify_build
from collector_engineering.playbook_v2 import build_typed_ir, load_playbook_v2

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


if __name__ == "__main__":
    unittest.main()
