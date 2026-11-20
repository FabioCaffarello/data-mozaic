import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from collector_engineering.playbooks import (
    PlaybookError,
    _PrivateRawOutput,
    accept_acquisition,
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
            temporary_path = Path(temporary)
            playbook_path = temporary_path / "candidate.yaml"
            playbook_path.write_text(rendered)
            reparsed = load_playbook(playbook_path)
            report = replay(reparsed, MULTIPAGE, temporary_path / "replay-output")
            self.assertEqual(2, len(report["raw_outputs"]))
            first_body = (
                temporary_path / "replay-output" / report["raw_outputs"][0]["body_file"]
            ).read_bytes()
            self.assertIn(b'data-id="r-101"', first_body)
            second_report = replay(
                reparsed, MULTIPAGE, temporary_path / "replay-output"
            )
            self.assertEqual(report, second_report)
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
        self.assertTrue(report["raw_manifest"]["complete"])
        self.assertEqual(
            "raw/index.json",
            report["raw_manifest"]["path"],
        )

    def test_replay_rejects_symlink_output_directory_without_external_write(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output_root = base / "authorized output"
            output_root.mkdir(mode=0o700)
            outside = base / "outside"
            outside.mkdir(mode=0o700)
            (output_root / "raw").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(PlaybookError, "secure raw output"):
                replay(draft, MULTIPAGE, output_root)
            self.assertEqual([], list(outside.iterdir()))

    def test_replay_rejects_symlink_in_output_root_components(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir(mode=0o700)
            link = base / "redirect"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(PlaybookError, "secure raw output"):
                replay(draft, MULTIPAGE, link / "results")
            self.assertEqual([], list(outside.iterdir()))

    def test_replay_detects_raw_directory_replacement_during_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output_root = base / "output"
            output_root.mkdir(mode=0o700)
            outside = base / "outside"
            outside.mkdir(mode=0o700)
            publisher = _PrivateRawOutput(output_root)
            publisher.__enter__()
            try:
                (output_root / "raw").rename(output_root / "held-raw")
                (output_root / "raw").symlink_to(outside, target_is_directory=True)
                publisher.publish_body("race.body", b"synthetic raw bytes")
                with self.assertRaisesRegex(PlaybookError, "changed during replay"):
                    publisher.publish_manifest(b'{"complete":true}\n')
            finally:
                publisher.close()
            self.assertEqual([], list(outside.iterdir()))
            self.assertFalse((output_root / "held-raw/index.json").exists())

    def test_trusted_direct_replay_documents_same_uid_rename_limit(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output_root = base / "output"
            output_root.mkdir(mode=0o700)
            outside = base / "outside"
            outside.mkdir(mode=0o700)
            moved_raw = outside / "moved-raw"
            real_write = os.write
            moved = False

            def move_raw_then_write(descriptor, content):
                nonlocal moved
                if not moved:
                    moved = True
                    (output_root / "raw").rename(moved_raw)
                    (output_root / "raw").symlink_to(outside, target_is_directory=True)
                return real_write(descriptor, content)

            with (
                mock.patch(
                    "collector_engineering.playbooks.os.write",
                    move_raw_then_write,
                ),
                self.assertRaisesRegex(
                    PlaybookError, "raw output directory changed during replay"
                ),
            ):
                replay(draft, MULTIPAGE, output_root)

            self.assertTrue((output_root / "raw").is_symlink())
            self.assertEqual([moved_raw], list(outside.iterdir()))
            published = list(moved_raw.iterdir())
            self.assertEqual(2, len(published))
            self.assertEqual({136, 104}, {path.stat().st_size for path in published})

    def test_replay_rejects_final_symlink_fifo_and_existing_arbitrary_file(self):
        draft = build_draft(self.transcript)
        body_digest = json.loads((MULTIPAGE / "index.json").read_text())["pages"][0][
            "sha256"
        ]
        output_name = f"0001-{body_digest}.body"
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output_root = base / "symlink-case"
            output_root.mkdir(mode=0o700)
            raw = output_root / "raw"
            raw.mkdir(mode=0o700)
            outside = base / "outside.body"
            outside.write_bytes(b"do not overwrite")
            (raw / output_name).symlink_to(outside)
            with self.assertRaises(PlaybookError):
                replay(draft, MULTIPAGE, output_root)
            self.assertEqual(b"do not overwrite", outside.read_bytes())

            fifo_root = base / "fifo-case"
            fifo_root.mkdir(mode=0o700)
            fifo_raw = fifo_root / "raw"
            fifo_raw.mkdir(mode=0o700)
            os.mkfifo(fifo_raw / output_name, mode=0o600)
            with self.assertRaises(PlaybookError):
                replay(draft, MULTIPAGE, fifo_root)

            existing_root = base / "existing-case"
            existing_root.mkdir(mode=0o700)
            existing_raw = existing_root / "raw"
            existing_raw.mkdir(mode=0o700)
            existing = existing_raw / output_name
            existing.write_bytes(b"arbitrary preexisting file")
            existing.chmod(0o600)
            with self.assertRaises(PlaybookError):
                replay(draft, MULTIPAGE, existing_root)
            self.assertEqual(b"arbitrary preexisting file", existing.read_bytes())

    def test_replay_rejects_permissive_root_and_raw_directories(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "permissive-root"
            root.mkdir(mode=0o700)
            root.chmod(0o755)
            with self.assertRaisesRegex(PlaybookError, "owner-only"):
                replay(draft, MULTIPAGE, root)

            raw_root = base / "permissive-raw"
            (raw_root / "raw").mkdir(parents=True, mode=0o700)
            (raw_root / "raw").chmod(0o755)
            with self.assertRaisesRegex(PlaybookError, "owner-only"):
                replay(draft, MULTIPAGE, raw_root)

    def test_replay_partial_write_publishes_no_index_and_can_retry(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "output with spaces"
            real_write = os.write
            calls = 0

            def partial_then_fail(descriptor, content):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_write(descriptor, content[:2])
                raise OSError("injected partial write")

            with (
                mock.patch(
                    "collector_engineering.playbooks.os.write", partial_then_fail
                ),
                self.assertRaisesRegex(PlaybookError, "publication failed"),
            ):
                replay(draft, MULTIPAGE, output_root)
            raw = output_root / "raw"
            self.assertFalse((raw / "index.json").exists())
            self.assertEqual([], list(raw.glob(".pending-*")))
            report = replay(draft, MULTIPAGE, output_root)
            self.assertEqual("passed", report["state"])
            self.assertTrue((raw / "index.json").is_file())
            self.assertEqual(0o700, stat.S_IMODE(raw.stat().st_mode))

    def test_replay_reexport_rejects_digest_divergence(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "out"
            replay(draft, MULTIPAGE, output_root)
            output = (
                output_root
                / "raw"
                / json.loads((output_root / "raw/index.json").read_text())["outputs"][
                    0
                ]["body_file"].removeprefix("raw/")
            )
            output.write_bytes(b"tampered bytes")
            with self.assertRaises(PlaybookError):
                replay(draft, MULTIPAGE, output_root)

    def test_replay_rejects_group_or_world_writable_output_ancestor(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "permissive parent"
            parent.mkdir(mode=0o700)
            parent.chmod(0o777)
            output_root = parent / "output"
            with self.assertRaisesRegex(PlaybookError, "ancestors"):
                replay(draft, MULTIPAGE, output_root)
            self.assertFalse(output_root.exists())

    def test_replay_serializes_concurrent_publication_and_is_idempotent(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "concurrent output"
            with ThreadPoolExecutor(max_workers=2) as pool:
                reports = list(
                    pool.map(
                        lambda _: replay(draft, MULTIPAGE, output_root),
                        range(2),
                    )
                )
            self.assertEqual(reports[0], reports[1])
            index = json.loads((output_root / "raw/index.json").read_text())
            self.assertTrue(index["complete"])
            self.assertEqual(2, len(index["outputs"]))

    def test_replay_rejects_raw_index_symlink_and_keeps_retryable_outputs(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            output_root = base / "output"
            raw = output_root / "raw"
            output_root.mkdir(mode=0o700)
            raw.mkdir(mode=0o700)
            outside = base / "outside-index"
            outside.write_bytes(b"not an index")
            (raw / "index.json").symlink_to(outside)
            with self.assertRaises(PlaybookError):
                replay(draft, MULTIPAGE, output_root)
            self.assertEqual(b"not an index", outside.read_bytes())
            (raw / "index.json").unlink()
            report = replay(draft, MULTIPAGE, output_root)
            self.assertEqual("passed", report["state"])

    def test_replay_interrupted_before_manifest_has_no_success_index_and_retries(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "interrupted output"
            with (
                mock.patch.object(
                    _PrivateRawOutput,
                    "publish_manifest",
                    side_effect=OSError("injected manifest interruption"),
                ),
                self.assertRaisesRegex(PlaybookError, "publication failed"),
            ):
                replay(draft, MULTIPAGE, output_root)
            raw = output_root / "raw"
            self.assertFalse((raw / "index.json").exists())
            self.assertEqual(2, len(list(raw.glob("*.body"))))
            report = replay(draft, MULTIPAGE, output_root)
            self.assertEqual("passed", report["state"])
            self.assertTrue(json.loads((raw / "index.json").read_text())["complete"])

    def test_replay_removes_private_staging_file_left_by_process_interruption(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "interrupted process output"
            raw = output_root / "raw"
            raw.mkdir(parents=True, mode=0o700)
            output_root.chmod(0o700)
            stale = raw / ".pending-process-interruption"
            stale.write_bytes(b"partial")
            stale.chmod(0o600)
            report = replay(draft, MULTIPAGE, output_root)
            self.assertEqual("passed", report["state"])
            self.assertFalse(stale.exists())
            self.assertTrue((raw / "index.json").is_file())

    def test_replay_rejects_unsafe_stale_staging_entry(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "unsafe pending output"
            raw = output_root / "raw"
            raw.mkdir(parents=True, mode=0o700)
            output_root.chmod(0o700)
            external = Path(temporary) / "outside-pending-target"
            external.write_bytes(b"must remain untouched")
            (raw / ".pending-attacker").symlink_to(external)
            with self.assertRaises(PlaybookError):
                replay(draft, MULTIPAGE, output_root)
            self.assertEqual(b"must remain untouched", external.read_bytes())
            self.assertFalse((raw / "index.json").exists())

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
        with self.assertRaisesRegex(PlaybookError, "unsupported Scout transcript"):
            build_draft(self.transcript)
        self.transcript = json.loads((MULTIPAGE / "transcript.json").read_text())
        self.transcript["pages"][0]["evidence_ref"] = "invented-manifest"
        with self.assertRaisesRegex(PlaybookError, "evidence reference"):
            build_draft(self.transcript)

    def test_draft_uses_explicit_start_and_sequence_not_observation_id_order(self):
        self.transcript["pages"][0]["observation_id"] = "z-start"
        self.transcript["pages"][1]["observation_id"] = "a-next"
        self.transcript["pages"][1]["discovered_from_observation_ids"] = ["z-start"]
        self.assertEqual(
            "https://catalog.test/",
            build_draft(self.transcript)["steps"][0]["url"],
        )

    def test_draft_rejects_incomplete_and_unreachable_transcripts(self):
        incomplete = json.loads(json.dumps(self.transcript))
        incomplete["navigation"]["sequence_complete"] = False
        with self.assertRaisesRegex(PlaybookError, "incomplete transcript"):
            build_draft(incomplete)
        unreachable = json.loads(json.dumps(self.transcript))
        unreachable["pages"][0]["discovered_links"] = []
        unreachable["pages"][1]["discovered_from_observation_ids"] = []
        with self.assertRaisesRegex(PlaybookError, "discovery relations|not reachable"):
            build_draft(unreachable)

    def test_draft_preserves_redirected_start_url(self):
        redirected = json.loads(json.dumps(self.transcript))
        redirected["navigation"]["start_requested_url"] = "https://catalog.test/start"
        redirected["pages"][0]["requested_url"] = "https://catalog.test/start"
        self.assertEqual(
            "https://catalog.test/start",
            build_draft(redirected)["steps"][0]["url"],
        )
        draft = build_draft(redirected)
        with tempfile.TemporaryDirectory() as temporary:
            fixture_copy = Path(temporary) / "fixtures"
            shutil.copytree(MULTIPAGE, fixture_copy)
            index = json.loads((fixture_copy / "index.json").read_text())
            index["pages"][0]["requested_url"] = "https://catalog.test/start"
            (fixture_copy / "index.json").write_text(json.dumps(index))
            result = replay(draft, fixture_copy, Path(temporary) / "output")
        self.assertEqual("passed", result["state"])
        self.assertEqual(
            "https://catalog.test/start", result["replayed_pages"][0]["requested_url"]
        )
        self.assertEqual("https://catalog.test/", result["replayed_pages"][0]["url"])

    def test_draft_rejects_ambiguous_start_duplicate_resources_and_bad_strategy(self):
        missing_start = json.loads(json.dumps(self.transcript))
        missing_start["navigation"]["start_requested_url"] = (
            "https://catalog.test/missing"
        )
        with self.assertRaisesRegex(PlaybookError, "starting observation"):
            build_draft(missing_start)
        duplicate = json.loads(json.dumps(self.transcript))
        duplicate["pages"][1]["url"] = duplicate["pages"][0]["url"]
        duplicate["pages"][1]["requested_url"] = duplicate["pages"][0]["requested_url"]
        with self.assertRaisesRegex(
            PlaybookError, "discovery relations|duplicate page URL"
        ):
            build_draft(duplicate)
        unsupported = json.loads(json.dumps(self.transcript))
        unsupported["proposal"]["required_capabilities"] = ["browser.execute"]
        with self.assertRaisesRegex(PlaybookError, "unsupported draft/v1 capabilities"):
            build_draft(unsupported)

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

    def test_semantic_validation_rejects_unreachable_or_missing_raw_output(self):
        draft = build_draft(self.transcript)
        draft["steps"][1]["from_step"] = "unrelated"
        self.assertTrue(any("unreachable" in item for item in validate_playbook(draft)))
        draft = build_draft(self.transcript)
        draft["steps"].pop()
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
            report = replay(draft, Path(temporary), Path(temporary) / "output")
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
                replay(draft, Path(temporary), Path(temporary) / "output")

    def test_replay_deduplicates_revisited_resources(self):
        draft = build_draft(self.transcript)
        with tempfile.TemporaryDirectory() as temporary:
            fixture_copy = Path(temporary) / "fixtures"
            shutil.copytree(MULTIPAGE, fixture_copy)
            index_path = fixture_copy / "index.json"
            index = json.loads(index_path.read_text())
            body_path = fixture_copy / index["pages"][1]["body_file"]
            body = body_path.read_bytes().replace(
                b"</body>",
                b'<a href="/">Home</a><a href="/">Home again</a></body>',
            )
            body_path.write_bytes(body)
            index["pages"][1]["sha256"] = hashlib.sha256(body).hexdigest()
            index_path.write_text(json.dumps(index))
            report = replay(draft, fixture_copy, Path(temporary) / "output")
        self.assertEqual("passed", report["state"])
        self.assertEqual(2, len(report["replayed_pages"]))

    def test_acquisition_acceptance_uses_independent_expectations(self):
        draft = build_draft(self.transcript)
        expectations = json.loads((MULTIPAGE / "acceptance.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            result = accept_acquisition(
                draft,
                self.transcript,
                MULTIPAGE,
                Path(temporary) / "output",
                expectations,
            )
        self.assertEqual("passed", result["structural_validation"]["state"])
        self.assertEqual("passed", result["replay_execution"]["state"])
        self.assertEqual("accepted", result["acquisition_acceptance"]["state"])
        self.assertFalse(result["acquisition_acceptance"]["records_extracted"])

    def test_acceptance_rejects_wrong_start_even_when_replay_passes(self):
        draft = build_draft(self.transcript)
        expectations = json.loads((MULTIPAGE / "acceptance.json").read_text())
        draft["steps"][0]["url"] = "https://catalog.test/items/page-2"
        with tempfile.TemporaryDirectory() as temporary:
            result = accept_acquisition(
                draft,
                self.transcript,
                MULTIPAGE,
                Path(temporary) / "output",
                expectations,
            )
        self.assertEqual("passed", result["replay_execution"]["state"])
        self.assertEqual("rejected", result["acquisition_acceptance"]["state"])
        self.assertTrue(
            any(
                "requested URL" in item
                for item in result["acquisition_acceptance"]["failures"]
            )
        )

    def test_acceptance_rejects_empty_or_unbounded_independent_expectations(self):
        draft = build_draft(self.transcript)
        expectations = json.loads((MULTIPAGE / "acceptance.json").read_text())
        expectations["required_pages"] = []
        expectations["max_requests"] = 0
        expectations["max_pages"] = True
        with tempfile.TemporaryDirectory() as temporary:
            result = accept_acquisition(
                draft,
                self.transcript,
                MULTIPAGE,
                Path(temporary) / "output",
                expectations,
            )
        self.assertEqual("passed", result["replay_execution"]["state"])
        self.assertEqual("rejected", result["acquisition_acceptance"]["state"])
        self.assertEqual(
            {
                "independent expectations must require at least one page",
                "independent request limit must be a positive integer",
                "independent page limit must be a positive integer",
            },
            set(result["acquisition_acceptance"]["failures"]),
        )


if __name__ == "__main__":
    unittest.main()
