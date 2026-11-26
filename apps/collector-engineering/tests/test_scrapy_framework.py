from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector_engineering import reference_runtime, scrapy_framework


class ScrapyFrameworkLifecycleTests(unittest.TestCase):
    def test_input_query_substitution_is_single_key_and_encoded(self):
        fetch = {
            "inputs": {
                "url": "https://controlled.invalid/items?limit=1",
                "input_query": {"parameter": "limit", "query_name": "limit"},
            }
        }
        self.assertEqual(
            "https://controlled.invalid/items?limit=3",
            scrapy_framework._live_start_url(fetch, {"limit": 3}),
        )
        with self.assertRaisesRegex(
            reference_runtime.RuntimeFailure, "INPUT_PARAMETERS_INVALID"
        ):
            scrapy_framework._live_start_url(fetch, {"limit": 3, "other": 1})

    def test_input_query_substitution_rejects_ambiguous_compiled_queries(self):
        fetch = {
            "inputs": {
                "url": "https://controlled.invalid/items?limit=1&limit=2",
                "input_query": {"parameter": "limit", "query_name": "limit"},
            }
        }
        with self.assertRaisesRegex(
            reference_runtime.RuntimeFailure, "INPUT_QUERY_PLAN_INVALID"
        ):
            scrapy_framework._live_start_url(fetch, {"limit": 3})

    def test_input_parameter_file_must_match_the_bc04_capability_digest(self):
        plan = {
            "fetch": {
                "inputs": {
                    "input_query": {
                        "parameter": "limit",
                        "query_name": "limit",
                    }
                }
            }
        }
        with tempfile.TemporaryDirectory(prefix="dm-input-parameters-") as folder:
            root = Path(folder)
            parameters = root / "parameters.json"
            capability = root / "capability"
            body = b'{"limit":3}'
            parameters.write_bytes(body)
            parameters.chmod(0o400)
            claims = {
                "input_parameters_digest": "sha256:" + hashlib.sha256(body).hexdigest()
            }
            capability.write_text(
                base64.urlsafe_b64encode(json.dumps(claims).encode())
                .decode()
                .rstrip("=")
            )
            capability.chmod(0o400)
            with patch.dict(
                os.environ, {"MOZAIC_EXECUTION_CAPABILITY_FILE": str(capability)}
            ):
                self.assertEqual(
                    {"limit": 3},
                    scrapy_framework._read_live_input_parameters(plan, str(parameters)),
                )
                parameters.chmod(0o600)
                # This is still a contract-allowed value; only the specific
                # per-execution capability makes it unauthorized here.
                parameters.write_bytes(b'{"limit":2}')
                parameters.chmod(0o400)
                with self.assertRaisesRegex(
                    reference_runtime.RuntimeFailure,
                    "INPUT_PARAMETERS_UNAUTHORIZED",
                ):
                    scrapy_framework._read_live_input_parameters(plan, str(parameters))

    def test_parameterless_artifact_input_must_match_empty_bc04_binding(self):
        target = "https://controlled.invalid/files/report.pdf"
        plan = {
            "artifact_output": {"operator_type": "output.artifact"},
            "fetch": {"inputs": {"url": target}},
        }
        with tempfile.TemporaryDirectory(prefix="dm-empty-artifact-input-") as folder:
            root = Path(folder)
            parameters = root / "parameters.json"
            capability = root / "capability"
            body = b"{}"
            parameters.write_bytes(body)
            parameters.chmod(0o400)
            capability.write_text(
                base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "input_parameters_digest": "sha256:"
                            + hashlib.sha256(body).hexdigest(),
                            "expected_initial_target_digest": "sha256:"
                            + hashlib.sha256(target.encode()).hexdigest(),
                        }
                    ).encode()
                )
                .decode()
                .rstrip("=")
            )
            capability.chmod(0o400)
            with patch.dict(
                os.environ, {"MOZAIC_EXECUTION_CAPABILITY_FILE": str(capability)}
            ):
                self.assertEqual(
                    {},
                    scrapy_framework._read_live_input_parameters(plan, str(parameters)),
                )
                self.assertEqual(
                    target, scrapy_framework._live_start_url(plan["fetch"], {})
                )
                parameters.chmod(0o400)
                parameters.chmod(0o600)
                parameters.write_bytes(b'{"limit":1}')
                parameters.chmod(0o400)
                with self.assertRaisesRegex(
                    reference_runtime.RuntimeFailure, "INPUT_PARAMETERS_UNAUTHORIZED"
                ):
                    scrapy_framework._read_live_input_parameters(plan, str(parameters))

    def _assert_initialization_roots_are_removed(self, create_spider) -> None:
        original_mkdtemp = tempfile.mkdtemp
        created: list[Path] = []

        def make_temp(*args, **kwargs):
            root = Path(original_mkdtemp(*args, **kwargs))
            created.append(root)
            return str(root)

        with (
            patch.object(scrapy_framework.tempfile, "mkdtemp", side_effect=make_temp),
            patch.object(
                reference_runtime.RuntimeContext,
                "create",
                side_effect=RuntimeError("injected runtime initialization failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected runtime"),
        ):
            create_spider()

        self.assertTrue(created)
        self.assertTrue(all(not root.exists() for root in created))

    def test_fixture_spider_cleans_context_when_runtime_initialization_fails(self):
        with tempfile.TemporaryDirectory(prefix="dm-framework-test-") as directory:
            fixture_root = Path(directory) / "fixtures"
            fixture_root.mkdir()
            plan = {
                "source": {},
                "budgets": {},
                "fixtures": [],
                "fetch": {"inputs": {"url": "https://fixture.invalid/"}},
                "select": {},
                "extract": {},
                "discover": None,
                "page_limit": 1,
                "raw_output": False,
            }
            self._assert_initialization_roots_are_removed(
                lambda: scrapy_framework.FixturePlanSpider(
                    plan, str(fixture_root), str(Path(directory) / "output")
                )
            )

    def test_live_spider_cleans_context_when_runtime_initialization_fails(self):
        plan = {
            "source": {},
            "budgets": {"max_pages": 1},
            "fetch": {"inputs": {"url": "https://controlled.invalid/"}},
            "select": {},
            "extract": {},
            "discover": None,
            "pagination": None,
        }
        self._assert_initialization_roots_are_removed(
            lambda: scrapy_framework.LivePlanSpider(plan, "/tmp/output")
        )


if __name__ == "__main__":
    unittest.main()
