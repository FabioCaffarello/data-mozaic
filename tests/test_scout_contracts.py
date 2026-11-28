"""Contract conformance for illustrative BC14 draft payloads."""

import copy
import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
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

    def test_playbook_proposal_v2_is_additive_and_requires_candidate_digest(self):
        schema = read(
            Path(__file__).resolve().parents[1]
            / "contracts/scout/v2/PlaybookProposal.schema.json"
        )
        Draft202012Validator.check_schema(schema)
        legacy = read(ROOT / "fixtures/valid/PlaybookProposal.json")
        candidate = {"dsl_version": "capture-playbook-draft/v4"}
        encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n"
        payload = {
            **legacy,
            "contract_version": "2.0.0",
            "proposal_revision": 1,
            "candidate_specification": candidate,
            "candidate_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
        }
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(payload)))
        del payload["candidate_sha256"]
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(payload)))

    def test_transcript_v4_preserves_v3_shape_and_candidate_digest(self):
        root = Path(__file__).resolve().parents[1]
        payload = read(
            root / "contracts/scout/transcript/draft/v2/fixtures/valid/multipage.json"
        )
        payload["transcript_schema"] = "scout-transcript-draft/v4"
        for page in payload["pages"]:
            page["content_type"] = "application/json"
        proposal = payload["proposal"]
        proposal["contract_version"] = "2.0.0"
        candidate = {"dsl_version": "capture-playbook-draft/v4"}
        proposal.update(
            proposal_revision=1,
            candidate_specification=candidate,
            candidate_sha256=hashlib.sha256(
                (
                    json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode()
            ).hexdigest(),
        )
        payload["proposal"] = proposal
        payload["proposal_history"] = [
            {"revision": 1, "proposal": copy.deepcopy(proposal)}
        ]
        schema = read(
            root
            / "contracts/scout/transcript/draft/v4/ScoutSessionTranscript.schema.json"
        )
        Draft202012Validator.check_schema(schema)
        validate_transcript(payload)
        payload["proposal"]["candidate_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "candidate digest"):
            validate_transcript(payload)

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

    def test_transcript_v2_navigation_sequence_and_relations(self):
        root = Path(__file__).resolve().parents[1]
        v2 = root / "contracts/scout/transcript/draft/v2"
        schema = read(v2 / "ScoutSessionTranscript.schema.json")
        Draft202012Validator.check_schema(schema)
        payload = read(v2 / "fixtures/valid/multipage.json")
        validate_transcript(payload)
        reordered_ids = copy.deepcopy(payload)
        reordered_ids["pages"][0]["observation_id"] = "z-start"
        reordered_ids["pages"][1]["observation_id"] = "a-next"
        reordered_ids["pages"][1]["discovered_from_observation_ids"] = ["z-start"]
        validate_transcript(reordered_ids)
        wrong_sequence = copy.deepcopy(payload)
        wrong_sequence["pages"][1]["sequence"] = 1
        with self.assertRaisesRegex(ValueError, "navigation sequence"):
            validate_transcript(wrong_sequence)
        wrong_relation = copy.deepcopy(payload)
        wrong_relation["pages"][1]["discovered_from_observation_ids"] = []
        with self.assertRaisesRegex(ValueError, "discovery relation"):
            validate_transcript(wrong_relation)

    def test_transcript_v3_requires_declared_media_type_without_reinterpreting_v2(self):
        root = Path(__file__).resolve().parents[1]
        v3 = root / "contracts/scout/transcript/draft/v3"
        schema = read(v3 / "ScoutSessionTranscript.schema.json")
        Draft202012Validator.check_schema(schema)
        payload = read(
            root / "contracts/scout/transcript/draft/v2/fixtures/valid/multipage.json"
        )
        payload["transcript_schema"] = "scout-transcript-draft/v3"
        for page in payload["pages"]:
            page["content_type"] = "application/json"
        validate_transcript(payload)
        invalid = copy.deepcopy(payload)
        del invalid["pages"][0]["content_type"]
        with self.assertRaises(ValidationError):
            validate_transcript(invalid)

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
