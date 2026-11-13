"""Contract conformance for illustrative BC14 draft payloads."""

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from scout.edge import validate_transcript

ROOT = Path(__file__).resolve().parents[1] / "contracts" / "scout" / "v1"
TRANSCRIPTS = ROOT.parent / "transcript" / "draft" / "v1"
NAMES = ("ScoutRequest", "EvidenceManifest", "DiscoveryFinding", "PlaybookProposal")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ScoutContractTests(unittest.TestCase):
    def test_schemas_and_fixtures(self):
        for name in NAMES:
            with self.subTest(contract=name):
                schema = read(ROOT / f"{name}.schema.json")
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema, format_checker=FormatChecker())
                valid = read(ROOT / "fixtures" / "valid" / f"{name}.json")
                invalid = read(ROOT / "fixtures" / "invalid" / f"{name}.json")
                self.assertEqual(list(validator.iter_errors(valid)), [])
                self.assertTrue(list(validator.iter_errors(invalid)))
                extra = copy.deepcopy(valid)
                extra["unexpected"] = True
                self.assertTrue(list(validator.iter_errors(extra)))
                missing = copy.deepcopy(valid)
                del missing["contract_version"]
                self.assertTrue(list(validator.iter_errors(missing)))

    def test_transcript_contract_and_cross_reference_fixtures(self):
        schema = read(TRANSCRIPTS / "ScoutSessionTranscript.schema.json")
        Draft202012Validator.check_schema(schema)
        valid = read(TRANSCRIPTS / "fixtures/valid/multipage.json")
        validate_transcript(valid)
        invalid = read(TRANSCRIPTS / "fixtures/invalid/evidence-digest-mismatch.json")
        with self.assertRaisesRegex(ValueError, "evidence reference mismatch"):
            validate_transcript(invalid)
        altered_approval = copy.deepcopy(valid)
        altered_approval["approval_record"]["origin"] = "https://other.test"
        with self.assertRaisesRegex(ValueError, "approval references mismatch"):
            validate_transcript(altered_approval)
        invented_ref = copy.deepcopy(valid)
        invented_ref["pages"][0]["evidence_ref"] = "invented-manifest"
        with self.assertRaisesRegex(ValueError, "evidence reference mismatch"):
            validate_transcript(invented_ref)

    def test_security_and_evidence_constraints(self):
        cases = [
            ("ScoutRequest", lambda p: p["requested_scope"].update(methods=["POST"])),
            ("ScoutRequest", lambda p: p["requested_scope"].update(max_requests=0)),
            ("EvidenceManifest", lambda p: p["artifacts"][0].update(sha256="bad")),
            ("EvidenceManifest", lambda p: p.update(sealed_at="not-a-dateZ")),
            ("DiscoveryFinding", lambda p: p.update(confidence=1.1)),
            ("DiscoveryFinding", lambda p: p.update(epistemic_status="proven")),
            ("PlaybookProposal", lambda p: p.update(contract_version="2.0.0")),
        ]
        for name, mutate in cases:
            with self.subTest(contract=name, mutation=mutate):
                payload = read(ROOT / "fixtures" / "valid" / f"{name}.json")
                mutate(payload)
                validator = Draft202012Validator(
                    read(ROOT / f"{name}.schema.json"), format_checker=FormatChecker()
                )
                self.assertTrue(list(validator.iter_errors(payload)))


if __name__ == "__main__":
    unittest.main()
