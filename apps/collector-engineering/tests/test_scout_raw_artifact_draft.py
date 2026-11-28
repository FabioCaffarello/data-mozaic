from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from collector_engineering.playbook_v2 import validate_playbook_v2
from collector_engineering.playbooks import (
    build_raw_artifact_v4_draft,
)
from collector_engineering.syntax import PlaybookError

ROOT = Path(__file__).resolve().parents[3]
TRANSCRIPT = ROOT / "contracts/scout/transcript/draft/v2/fixtures/valid/multipage.json"


def single_pdf_transcript() -> dict:
    transcript = json.loads(TRANSCRIPT.read_text())
    transcript["transcript_schema"] = "scout-transcript-draft/v3"
    transcript["pages"] = [transcript["pages"][0]]
    transcript["pages"][0]["content_type"] = "application/pdf"
    transcript["pages"][0]["discovered_links"] = []
    transcript["evidence"] = [transcript["evidence"][0]]
    transcript["findings"] = [
        {
            "classification": "observed",
            "evidence_refs": ["evidence-page-1"],
            "finding_id": "finding-catalog-1",
        }
    ]
    transcript["proposal"]["finding_ids"] = ["finding-catalog-1"]
    transcript["proposal"]["evidence_manifest_ids"] = ["evidence-page-1"]
    transcript["proposal"]["required_capabilities"] = ["http.fetch", "output.raw"]
    transcript["navigation"]["observation_count"] = 1
    return transcript


class ScoutRawArtifactDraftTests(unittest.TestCase):
    def test_evidence_backed_single_page_produces_current_raw_v4_profile(self):
        draft = build_raw_artifact_v4_draft(single_pdf_transcript())
        self.assertEqual("capture-playbook-draft/v4", draft["dsl_version"])
        self.assertEqual("artifact", draft["output_contract"]["mode"])
        self.assertEqual(["application/pdf"], draft["output_contract"]["media_types"])
        self.assertEqual(100_000, draft["output_contract"]["max_bytes"])
        self.assertEqual(
            ["http.fetch/v1", "output.artifact/v1"],
            [item["op"] for item in draft["operations"]],
        )
        self.assertEqual(["evidence-page-1"], draft["operations"][0]["evidence_refs"])
        self.assertTrue(validate_playbook_v2(draft).valid)

    def test_raw_profile_rejects_multiple_pages_redirects_bad_media_and_oversize(self):
        cases = []
        multiple = single_pdf_transcript()
        multiple["pages"].append(copy.deepcopy(multiple["pages"][0]))
        cases.append((multiple, "exactly one discovered page"))
        redirected = single_pdf_transcript()
        redirected["pages"][0]["requested_url"] = "https://catalog.test/alias"
        cases.append((redirected, "does not support redirects"))
        bad_media = single_pdf_transcript()
        bad_media["pages"][0]["content_type"] = "*/*"
        cases.append((bad_media, "valid exact declaration"))
        oversize = single_pdf_transcript()
        oversize["pages"][0]["size_bytes"] = 100_001
        cases.append((oversize, "exceeds the approved raw byte bound"))
        for transcript, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(PlaybookError, message),
            ):
                build_raw_artifact_v4_draft(transcript)


if __name__ == "__main__":
    unittest.main()
