"""Filesystem boundary checks for BC14 fixture publication by the MCP adapter."""

import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from scout.application import ReplayFixturePage
from scout.tool_host.server import _write_replay_fixtures


class ReplayFixtureExportTests(unittest.TestCase):
    def page(self):
        body = b"<html><body><article>record-1</article></body></html>"
        digest = hashlib.sha256(body).hexdigest()
        return ReplayFixturePage(
            url="https://catalog.test/docs/page",
            status=200,
            content_type="text/html",
            evidence_ref="manifest-1",
            artifact_id="artifact-1",
            sha256=digest,
            size_bytes=len(body),
            body=body,
            requested_url="https://catalog.test/docs/start",
            sequence=1,
            request_count=2,
        )

    def test_complete_publication_is_atomic_idempotent_and_concurrent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "private"
            root.mkdir(mode=0o700)
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda _: _write_replay_fixtures(
                            [self.page()], root, "session-1"
                        ),
                        range(2),
                    )
                )
            self.assertEqual(results[0], results[1])
            fixture_root, index_digest = results[0]
            index_bytes = (fixture_root / "index.json").read_bytes()
            self.assertEqual(index_digest, hashlib.sha256(index_bytes).hexdigest())
            index = json.loads(index_bytes)
            self.assertTrue(index["complete"])
            page = index["pages"][0]
            self.assertEqual("https://catalog.test/docs/start", page["requested_url"])
            self.assertEqual(2, page["request_count"])
            self.assertEqual(
                0o600, (fixture_root / page["body_file"]).stat().st_mode & 0o777
            )
            self.assertEqual([], list(fixture_root.glob(".pending-*")))

    def test_symlink_ancestor_and_permissive_directories_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            outside = base / "outside"
            outside.mkdir(mode=0o700)
            link = base / "redirect"
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises((OSError, ValueError)):
                _write_replay_fixtures([self.page()], link / "root", "session-1")
            unsafe_root = base / "unsafe-root"
            unsafe_root.mkdir(mode=0o755)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                _write_replay_fixtures([self.page()], unsafe_root, "session-1")

    def test_failure_before_index_publication_leaves_no_visible_partial_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "private"
            root.mkdir(mode=0o700)
            with (
                mock.patch(
                    "scout.tool_host.server.os.link",
                    side_effect=OSError("injected I/O failure"),
                ),
                self.assertRaisesRegex(OSError, "injected I/O"),
            ):
                _write_replay_fixtures([self.page()], root, "session-1")
            fixture_root = root / "session-1/captured-fixtures"
            self.assertFalse((fixture_root / "index.json").exists())
            self.assertEqual([], list(fixture_root.glob(".pending-*")))
            # A retry verifies/reuses a complete immutable body and publishes the index.
            _write_replay_fixtures([self.page()], root, "session-1")
            self.assertTrue(
                json.loads((fixture_root / "index.json").read_text())["complete"]
            )

    def test_insecure_preexisting_body_or_index_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "private"
            root.mkdir(mode=0o700)
            fixture_root, _ = _write_replay_fixtures([self.page()], root, "session-1")
            body = fixture_root / f"{self.page().sha256}.body"
            body.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "ownership, mode"):
                _write_replay_fixtures([self.page()], root, "session-1")
            body.chmod(0o600)
            (fixture_root / "index.json").chmod(0o644)
            with self.assertRaisesRegex(ValueError, "index is incomplete or differs"):
                _write_replay_fixtures([self.page()], root, "session-1")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_preexisting_fifo_cannot_block_fixture_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "private"
            root.mkdir(mode=0o700)
            fixture_root, _ = _write_replay_fixtures([self.page()], root, "session-1")
            (fixture_root / "index.json").unlink()
            body = fixture_root / f"{self.page().sha256}.body"
            body.unlink()
            os.mkfifo(body, mode=0o600)
            with self.assertRaisesRegex(ValueError, "ownership, mode or digest"):
                _write_replay_fixtures([self.page()], root, "session-1")


if __name__ == "__main__":
    unittest.main()
