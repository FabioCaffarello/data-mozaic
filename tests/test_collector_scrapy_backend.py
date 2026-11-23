"""Security and determinism tests for BC15's Scrapy fixture backend."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from collector_engineering.backend_scrapy_fixture import (
    PROFILE,
    compile_build,
    generate_spider,
    verify_build,
)
from collector_engineering.playbook_v2 import build_typed_ir, load_playbook_v2
from collector_engineering.reference_runtime import RuntimeFailure
from collector_engineering.runtime_identity import runtime_bundle_files
from collector_engineering.scrapy_fixture_runtime import (
    REQUEST_META_KEYS,
    RequestGuardMiddleware,
    handler_settings,
)
from collector_engineering.syntax import PlaybookError
from scrapy import Request

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples/playbooks"
LOCKFILE = ROOT / "uv.lock"


class ScrapyFixtureBackendTests(unittest.TestCase):
    def _build(self, temporary: str, example: str = "html-simple") -> Path:
        folder = EXAMPLES / example
        ir = build_typed_ir(
            load_playbook_v2(folder / "capture-playbook.yaml"),
            fixture_root=folder / "fixtures",
        )
        output = Path(temporary).resolve() / "build"
        compile_build(
            ir,
            fixture_root=folder / "fixtures",
            output=output,
            lockfile=LOCKFILE,
        )
        return output

    def test_profile_is_fixture_only_and_does_not_claim_browser(self):
        self.assertIn("http.fetch.fixture", PROFILE["capabilities"])
        self.assertNotIn("browser.rendered_dom", PROFILE["capabilities"])
        self.assertEqual("scrapy-fixture", PROFILE["backend_id"])

    def test_spider_generation_is_deterministic_and_operation_specific(self):
        folder = EXAMPLES / "multipage-catalog"
        ir = build_typed_ir(
            load_playbook_v2(folder / "capture-playbook.yaml"),
            fixture_root=folder / "fixtures",
        )
        _, identity = runtime_bundle_files(
            ("reference_runtime.py", "syntax.py", "scrapy_fixture_runtime.py")
        )
        fixtures = json.loads((folder / "fixtures/index.json").read_text())["pages"]
        generated = generate_spider(ir, fixtures, identity)
        self.assertEqual(generated, generate_spider(ir, fixtures, identity))
        compile(generated, "generated_scrapy_spider.py", "exec")
        source = generated.decode()
        self.assertIn("class GeneratedSpider(Spider", source)
        self.assertIn("def _op_select_", source)
        self.assertIn("BUILD_RUNTIME_IMPORT_MISMATCH", source)
        self.assertIn("FETCH_OPERATION_ID", source)
        self.assertNotIn("exec(", source)

    def test_effective_scrapy_settings_are_closed_and_single_request(self):
        settings = handler_settings()
        self.assertEqual(1, settings["CONCURRENT_REQUESTS"])
        self.assertEqual(1, settings["CONCURRENT_REQUESTS_PER_DOMAIN"])
        self.assertFalse(settings["RETRY_ENABLED"])
        self.assertFalse(settings["REDIRECT_ENABLED"])
        self.assertIsNone(settings["JOBDIR"])
        self.assertEqual({}, settings["FEEDS"])
        self.assertEqual(
            "mozaic_bot_runtime.scrapy_fixture_runtime.FixtureOnlyDownloadHandler",
            settings["DOWNLOAD_HANDLERS"]["http"],
        )
        self.assertEqual(
            "mozaic_bot_runtime.scrapy_fixture_runtime.FixtureOnlyDownloadHandler",
            settings["DOWNLOAD_HANDLERS"]["https"],
        )
        enabled_schemes = {
            scheme
            for scheme, handler in settings["DOWNLOAD_HANDLERS"].items()
            if handler is not None
        }
        self.assertEqual({"http", "https"}, enabled_schemes)
        self.assertEqual(
            {
                "dm_operation",
                "dm_page_index",
                "dm_discovered_from",
                "download_slot",
                "_download_slot_stamp",
            },
            REQUEST_META_KEYS,
        )

    def test_request_guard_rejects_unsupported_schemes_and_agent_meta(self):
        guard = RequestGuardMiddleware()
        for url in (
            "file:///etc/passwd",
            "data:text/plain,hello",
            "ftp://fixture.test/",
        ):
            with (
                self.subTest(url=url),
                self.assertRaisesRegex(RuntimeFailure, "SCHEME_UNSUPPORTED"),
            ):
                guard.process_request(Request(url), None)
        with self.assertRaisesRegex(RuntimeFailure, "REQUEST_META_UNSUPPORTED"):
            guard.process_request(
                Request("https://fixture.test/", meta={"download_handler": "anything"}),
                None,
            )

    def test_build_verifier_detects_runtime_file_drift_missing_and_extra(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = self._build(temporary)
            self.assertEqual("scrapy-fixture", verify_build(build)["backend_id"])
            runtime = build / "runtime/mozaic_bot_runtime"
            original = (runtime / "reference_runtime.py").read_bytes()
            (runtime / "reference_runtime.py").write_bytes(original + b"\n# drift\n")
            with self.assertRaisesRegex(PlaybookError, "BUILD_RUNTIME_DIGEST_MISMATCH"):
                verify_build(build)
            (runtime / "reference_runtime.py").write_bytes(original)
            (runtime / "syntax.py").unlink()
            with self.assertRaisesRegex(
                PlaybookError, "BUILD_RUNTIME_FILE_SET_MISMATCH"
            ):
                verify_build(build)
            (runtime / "syntax.py").write_bytes(b"present again")
            with self.assertRaisesRegex(PlaybookError, "BUILD_RUNTIME_DIGEST_MISMATCH"):
                verify_build(build)

    def test_build_verifier_detects_runtime_addition_and_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            build = self._build(temporary)
            runtime = build / "runtime/mozaic_bot_runtime"
            (runtime / "extra.py").write_text("# extra\n")
            with self.assertRaisesRegex(
                PlaybookError, "BUILD_RUNTIME_FILE_SET_MISMATCH"
            ):
                verify_build(build)
            (runtime / "extra.py").unlink()
            manifest_path = build / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["runtime_identity"]["version"] = "collector-runtime/0.0.0"
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(PlaybookError, "BUILD_RUNTIME_UNSUPPORTED"):
                verify_build(build)

    def test_unsupported_capability_fails_before_build_publication(self):
        folder = EXAMPLES / "browser-capability-unsupported"
        ir = build_typed_ir(
            load_playbook_v2(folder / "capture-playbook.yaml"),
            fixture_root=folder / "fixtures",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "must-not-exist"
            with self.assertRaisesRegex(PlaybookError, "CAPABILITY_UNSUPPORTED"):
                compile_build(
                    ir,
                    fixture_root=folder / "fixtures",
                    output=output,
                    lockfile=LOCKFILE,
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
