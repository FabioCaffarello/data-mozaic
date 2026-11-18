import json
import tempfile
import unittest
from pathlib import Path

from collector_engineering.playbooks import (
    PlaybookError,
    build_draft,
    dump_yaml,
    load_playbook,
    replay,
    validate_playbook,
)

ROOT = Path(__file__).resolve().parents[1]
MULTIPAGE = ROOT / "tests/fixtures/collector-engineering/multipage"


class CapturePlaybookTests(unittest.TestCase):
    def setUp(self):
        self.transcript = json.loads((MULTIPAGE / "transcript.json").read_text())

    def test_golden_multipage_draft_validate_replay_report(self):
        draft = build_draft(self.transcript)
        self.assertEqual([], validate_playbook(draft))
        rendered = dump_yaml(draft)
        with tempfile.TemporaryDirectory() as temporary:
            playbook_path = Path(temporary) / "candidate.yaml"
            playbook_path.write_text(rendered)
            reparsed = load_playbook(playbook_path)
            report = replay(reparsed, MULTIPAGE)
        self.assertEqual("passed", report["state"])
        self.assertEqual(2, len(report["replayed_pages"]))
        self.assertEqual(
            ["evidence-page-1", "evidence-page-2"],
            [item["evidence_ref"] for item in report["replayed_pages"]],
        )
        self.assertEqual([], report["failures"])
        self.assertEqual("draft", draft["status"])
        self.assertEqual(
            {"observed", "hypothesized"},
            {item["classification"] for item in draft["findings"]},
        )

    def test_draft_requires_receipt_and_scope_to_match(self):
        self.transcript["approval_record"]["origin"] = "https://other.test"
        with self.assertRaisesRegex(PlaybookError, "receipt differs"):
            build_draft(self.transcript)

    def test_transcript_digest_and_evidence_references_are_cross_checked(self):
        self.transcript["pages"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(PlaybookError, "evidence reference"):
            build_draft(self.transcript)

    def test_draft_requires_versioned_transcript_snapshot(self):
        self.transcript["transcript_schema"] = "scout-transcript-draft/v9"
        with self.assertRaisesRegex(PlaybookError, "transcript draft v1"):
            build_draft(self.transcript)
        self.transcript = json.loads((MULTIPAGE / "transcript.json").read_text())
        self.transcript["pages"][0]["evidence_ref"] = "invented-manifest"
        with self.assertRaisesRegex(PlaybookError, "evidence reference"):
            build_draft(self.transcript)

    def test_draft_rejects_unreferenced_or_invented_finding(self):
        self.transcript["proposal"]["finding_ids"] = ["not-observed"]
        with self.assertRaisesRegex(PlaybookError, "missing finding"):
            build_draft(self.transcript)

    def test_validator_rejects_scope_expansion_and_unknown_capability(self):
        draft = build_draft(self.transcript)
        draft["steps"][0]["url"] = "https://other.test/items"
        self.assertTrue(
            any("origin differs" in error for error in validate_playbook(draft))
        )
        draft = build_draft(self.transcript)
        draft["capabilities"].append("browser.execute")
        self.assertTrue(validate_playbook(draft))

    def test_yaml_duplicate_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.yaml"
            path.write_text("status: draft\nstatus: approved\n")
            with self.assertRaisesRegex(PlaybookError, "duplicate YAML key"):
                load_playbook(path)
            path.write_text("shared: &shared value\ncopy: *shared\n")
            with self.assertRaisesRegex(PlaybookError, "anchors and aliases"):
                load_playbook(path)

    def test_dsl_does_not_allow_invented_selectors(self):
        draft = build_draft(self.transcript)
        draft["steps"][1]["selector"] = "article.product"
        self.assertTrue(validate_playbook(draft))

    def test_replay_reports_missing_page_and_rejects_bad_digest(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            index = json.loads((MULTIPAGE / "index.json").read_text())
            index["pages"] = index["pages"][:1]
            (Path(temporary) / "index.json").write_text(json.dumps(index))
            (Path(temporary) / "page-1.html").write_bytes(
                (MULTIPAGE / "page-1.html").read_bytes()
            )
            report = replay(draft, Path(temporary))
        self.assertEqual("failed", report["state"])
        self.assertTrue(
            any("no captured fixture" in item["reason"] for item in report["failures"])
        )

        with tempfile.TemporaryDirectory() as temporary:
            index = json.loads((MULTIPAGE / "index.json").read_text())
            index["pages"][1]["sha256"] = "0" * 64
            (Path(temporary) / "index.json").write_text(json.dumps(index))
            for body_name in ("page-1.html", "page-2.html"):
                (Path(temporary) / body_name).write_bytes(
                    (MULTIPAGE / body_name).read_bytes()
                )
            with self.assertRaisesRegex(PlaybookError, "digest mismatch"):
                replay(draft, Path(temporary))


if __name__ == "__main__":
    unittest.main()
