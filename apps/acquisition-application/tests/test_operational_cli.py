"""Configuration and document-boundary tests for the installed operator CLI."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from acquisition_application.operational_cli import _input_document, _parser


class OperationalCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.document = {
            "tenant_id": "tenant-controlled",
            "input_id": "controlled-input-1",
            "source_id": "controlled-source",
            "source_revision_id": "controlled-revision-1",
            "input_contract": {
                "contract_id": "controlled.input.limit",
                "version": "1.0.0",
                "digest": "sha256:" + "a" * 64,
            },
            "idempotency_scope": "operator-acceptance",
            "idempotency_key": "input-1",
            "release_id": "controlled-release-1",
            "parameters": {"limit": 1},
        }

    def test_accepts_exact_bounded_input_document(self) -> None:
        path = self.root / "input.json"
        path.write_text(json.dumps(self.document))
        self.assertEqual(_input_document(path), self.document)

    def test_rejects_unknown_properties_and_duplicate_keys(self) -> None:
        path = self.root / "input.json"
        path.write_text(json.dumps(self.document | {"authority": "caller-value"}))
        with self.assertRaisesRegex(ValueError, "fields do not match"):
            _input_document(path)
        path.write_text('{"tenant_id":"one","tenant_id":"two"}')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _input_document(path)

    def test_rejects_symlink_input(self) -> None:
        target = self.root / "target.json"
        target.write_text(json.dumps(self.document))
        link = self.root / "input.json"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "regular file"):
            _input_document(link)

    def test_parser_exposes_bounded_operator_operations(self) -> None:
        parser = _parser()
        args = parser.parse_args(["run", "--document", "input.json"])
        self.assertEqual(args.command, "run")
        args = parser.parse_args(
            ["execute", "--tenant-id", "tenant", "--submission-id", "submission"]
        )
        self.assertEqual(args.command, "execute")


if __name__ == "__main__":
    unittest.main()
