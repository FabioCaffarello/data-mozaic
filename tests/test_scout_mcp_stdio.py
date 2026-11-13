"""Official MCP STDIO client drives the complete offline authoring slice."""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from collector_engineering.__main__ import main as collector_cli
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


class ScoutMCPStdioTests(unittest.TestCase):
    def test_stdio_fixture_evidence_transcript_and_bc15_cli(self):
        async def exercise(custody_root: Path, artifact_root: Path):
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{ROOT / 'apps/scout'}:{ROOT}"
            env["SCOUT_TEST_CUSTODY_ROOT"] = str(custody_root)
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "tests.mcp_stdio_fixture"],
                env=env,
            )
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as client,
            ):
                initialized = await client.initialize()
                self.assertTrue(initialized.serverInfo.name)
                listed = await client.list_tools()
                by_name = {tool.name: tool for tool in listed.tools}
                self.assertEqual(
                    set(by_name),
                    {
                        "scout_preflight",
                        "scout_start",
                        "scout_record_finding",
                        "scout_explore_fixture",
                        "scout_propose",
                        "scout_status",
                        "scout_export_transcript",
                    },
                )
                schema = by_name["scout_start"].inputSchema
                self.assertEqual(
                    set(schema["properties"]), {"source_id", "revision", "objective"}
                )
                self.assertFalse(
                    {"principal", "call_id", "approval", "budget", "path"}
                    & set(schema["properties"])
                )

                bad_args = await client.call_tool(
                    "scout_start",
                    {
                        "source_id": "catalog-test",
                        "revision": "revision-1",
                        "objective": "inspect fixture",
                        "principal": "attacker",
                    },
                )
                self.assertTrue(bad_args.isError)
                denied = await client.call_tool(
                    "scout_start",
                    {
                        "source_id": "unregistered",
                        "revision": "r1",
                        "objective": "inspect",
                    },
                )
                self.assertFalse(denied.isError)
                self.assertEqual(
                    json.loads(denied.content[0].text)["error"]["code"],
                    "authorization_denied",
                )

                started = await client.call_tool(
                    "scout_start",
                    {
                        "source_id": "catalog-test",
                        "revision": "revision-1",
                        "objective": "inspect the registered catalog fixture",
                    },
                )
                start_value = json.loads(started.content[0].text)
                self.assertTrue(start_value["ok"])
                session_id = start_value["session_id"]
                session_version = start_value["version"]

                manifest_ids = []
                for fixture_id in ("catalog-home", "catalog-page-2"):
                    result = await client.call_tool(
                        "scout_explore_fixture",
                        {
                            "session_id": session_id,
                            "expected_version": session_version,
                            "fixture_id": fixture_id,
                        },
                    )
                    value = json.loads(result.content[0].text)
                    self.assertTrue(value["ok"], value)
                    self.assertFalse(value["body_included"])
                    session_version = value["version"]
                    manifest_ids.append(value["manifest_id"])

                finding = await client.call_tool(
                    "scout_record_finding",
                    {
                        "session_id": session_id,
                        "expected_version": session_version,
                        "statement": "The fixture catalog links to its second page.",
                        "epistemic_status": "observation",
                        "confidence": 1.0,
                        "manifest_ids": manifest_ids,
                        "verification_note": "Observed in the retained fixture bytes.",
                    },
                )
                finding_value = json.loads(finding.content[0].text)
                self.assertTrue(finding_value["ok"], finding_value)
                session_version = finding_value["version"]
                finding_id = finding_value["finding_ids"][-1]

                proposal = await client.call_tool(
                    "scout_propose",
                    {
                        "session_id": session_id,
                        "expected_version": session_version,
                        "summary": "Capture the fixture catalog pages.",
                        "finding_ids": [finding_id],
                        "manifest_ids": manifest_ids,
                        "assumptions": ["Fixture behavior represents no live website."],
                        "risks": ["Local approval references are not signed."],
                        "required_capabilities": ["http.fetch", "links.discover"],
                    },
                )
                proposal_value = json.loads(proposal.content[0].text)
                self.assertTrue(proposal_value["ok"], proposal_value)
                self.assertEqual(proposal_value["handoff"], "delivery_pending")

                first_export = await client.call_tool(
                    "scout_export_transcript", {"session_id": session_id}
                )
                second_export = await client.call_tool(
                    "scout_export_transcript", {"session_id": session_id}
                )
                exported = json.loads(first_export.content[0].text)
                exported_again = json.loads(second_export.content[0].text)
                self.assertTrue(exported["ok"], exported)
                transcript_json = exported["transcript_json"]
                self.assertEqual(transcript_json, exported_again["transcript_json"])
                self.assertNotIn("disclose local files", transcript_json)
                encoded = transcript_json.encode("utf-8")
                self.assertEqual(
                    hashlib.sha256(encoded).hexdigest(), exported["sha256"]
                )
                transcript = json.loads(transcript_json)
                self.assertFalse(transcript["redaction"]["raw_content_included"])
                self.assertEqual(
                    "local-reference-not-cryptographically-signed",
                    transcript["integrity"]["approval_authentication"],
                )
                self.assertEqual(2, len(transcript["pages"]))
                self.assertEqual(2, len(transcript["evidence"]))

                artifact_root.mkdir(parents=True)
                transcript_path = artifact_root / "transcript.json"
                transcript_path.write_bytes(encoded)
                draft_path = artifact_root / "candidate.yaml"
                self.assertEqual(
                    0,
                    collector_cli(
                        ["draft", str(transcript_path), "--output", str(draft_path)]
                    ),
                )
                self.assertEqual(0, collector_cli(["validate", str(draft_path)]))

                fixture_dir = artifact_root / "fixtures"
                fixture_dir.mkdir()
                source_fixtures = ROOT / "apps/scout/scout/fixtures/mcp-catalog"
                index = {"pages": []}
                for page, fixture_id, filename in zip(
                    transcript["pages"],
                    ("catalog-home", "catalog-page-2"),
                    ("page-1.html", "page-2.html"),
                    strict=True,
                ):
                    body = (source_fixtures / f"{fixture_id}.html").read_bytes()
                    (fixture_dir / filename).write_bytes(body)
                    index["pages"].append(
                        {
                            "url": page["url"],
                            "status": 200,
                            "evidence_ref": page["evidence_ref"],
                            "body_file": filename,
                            "sha256": hashlib.sha256(body).hexdigest(),
                        }
                    )
                (fixture_dir / "index.json").write_text(
                    json.dumps(index, sort_keys=True), encoding="utf-8"
                )
                self.assertEqual(
                    0,
                    collector_cli(
                        [
                            "replay",
                            str(draft_path),
                            "--fixtures",
                            str(fixture_dir),
                            "--report",
                            str(artifact_root / "replay.json"),
                        ]
                    ),
                )
                replay_report = json.loads((artifact_root / "replay.json").read_text())
                self.assertEqual("passed", replay_report["state"])

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            asyncio.run(exercise(base / "custody", base / "artifacts"))


if __name__ == "__main__":
    unittest.main()
