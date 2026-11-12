"""Behavior checks for the repository documentation validator."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tooling import check_docs


class CheckDocsTests(unittest.TestCase):
    def test_local_links_and_fences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "exists.md").write_text("# Exists\n", encoding="utf-8")
            doc = root / "doc.md"
            doc.write_text(
                "[valid](exists.md)\n[external](https://example.com)\n"
                "```text\n[ignored](missing.md)\n```\n",
                encoding="utf-8",
            )
            with patch.object(check_docs, "ROOT", root):
                self.assertEqual(check_docs.check(doc), [])

    def test_missing_link_and_unclosed_fence_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc = root / "doc.md"
            doc.write_text("[broken](missing.md)\n```mermaid\n", encoding="utf-8")
            with patch.object(check_docs, "ROOT", root):
                errors = check_docs.check(doc)
            self.assertEqual(len(errors), 2)
            self.assertTrue(any("missing missing.md" in error for error in errors))
            self.assertTrue(any("unclosed code fence" in error for error in errors))
