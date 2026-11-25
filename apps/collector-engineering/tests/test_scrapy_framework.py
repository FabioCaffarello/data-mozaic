from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collector_engineering import reference_runtime, scrapy_framework


class ScrapyFrameworkLifecycleTests(unittest.TestCase):
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
