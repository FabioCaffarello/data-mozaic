"""Deterministic MCP-to-container BC15 composition test (requires Docker)."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from collector_engineering.playbooks import build_draft, dump_yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
MULTIPAGE = ROOT / "tests/fixtures/collector-engineering/multipage"


def _value(result):
    if result.isError:
        raise AssertionError(result.content[0].text)
    return json.loads(result.content[0].text)


def _remove_test_volumes(run_ids: list[str]) -> None:
    context = subprocess.run(
        ["docker", "context", "show"], capture_output=True, text=True, check=False
    ).stdout.strip()
    for run_id in run_ids:
        subprocess.run(
            [
                "docker",
                "--context",
                context,
                "volume",
                "rm",
                "--force",
                f"dm-bc15-{run_id}",
            ],
            capture_output=True,
            check=False,
        )


class ProtectedReplayCompositionTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("SCOUT_PROTECTED_REPLAY_E2E") == "1",
        "run through make scout-protected-replay-check with Docker",
    )
    def test_streamable_http_replay_and_independent_acceptance(self):
        run_ids = []
        self.addCleanup(_remove_test_volumes, run_ids)

        async def exercise(custody_root: Path):
            env = os.environ.copy()
            env["PYTHONPATH"] = f"{ROOT / 'apps/scout'}:{ROOT}"
            env["SCOUT_TEST_CUSTODY_ROOT"] = str(custody_root)
            env["SCOUT_TEST_REGISTRY_PATH"] = str(
                custody_root.parent / "bc15-runs.sqlite3"
            )
            env["SCOUT_TEST_WORKER_IMAGE_DIGEST"] = os.environ[
                "SCOUT_TEST_WORKER_IMAGE_DIGEST"
            ]
            env["SCOUT_TEST_BROKER_TOKEN"] = "synthetic-e2e-token-" + "b" * 48
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.mcp_http_fixture",
                cwd=ROOT,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            token = env["SCOUT_TEST_BROKER_TOKEN"]
            http_client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}
            )
            try:
                for _ in range(100):
                    if process.returncode is not None:
                        stderr = await process.stderr.read()
                        raise AssertionError(
                            f"HTTP fixture broker exited: {stderr.decode()}"
                        )
                    try:
                        response = await http_client.get(
                            "http://127.0.0.1:8765/healthz"
                        )
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("HTTP fixture broker did not become ready")
                async with (
                    streamable_http_client(
                        "http://127.0.0.1:8765/mcp", http_client=http_client
                    ) as (read, write, _),
                    ClientSession(read, write) as client,
                ):
                    await client.initialize()
                    listed = await client.list_tools()
                    names = {tool.name for tool in listed.tools}
                    self.assertIn("scout_bc15_replay", names)
                    self.assertIn("scout_bc15_accept", names)
                    self.assertNotIn("scout_export_replay_fixtures", names)
                    for name in (
                        "scout_bc15_replay",
                        "scout_bc15_accept",
                        "scout_bc15_status",
                        "scout_bc15_get_manifest",
                        "scout_bc15_get_report",
                    ):
                        schema = next(
                            tool.inputSchema
                            for tool in listed.tools
                            if tool.name == name
                        )
                        self.assertFalse(schema["additionalProperties"])
                        self.assertFalse(
                            {
                                "output_root",
                                "output_path",
                                "fixture_path",
                                "command",
                                "mount",
                            }
                            & set(schema["properties"])
                        )
                    replay_schema = next(
                        tool.inputSchema
                        for tool in listed.tools
                        if tool.name == "scout_bc15_replay"
                    )
                    self.assertEqual(
                        {"session_id", "draft_yaml"}, set(replay_schema["properties"])
                    )
                    arbitrary_path = await client.call_tool(
                        "scout_bc15_replay",
                        {
                            "session_id": "not-authorized",
                            "draft_yaml": "",
                            "output_root": "/tmp/attacker-selected",
                            "mount": "/:/host",
                            "image": "attacker/image:latest",
                            "command": ["sh", "-c", "id"],
                        },
                    )
                    self.assertTrue(arbitrary_path.isError)
                    wrong_revision = _value(
                        await client.call_tool(
                            "scout_preflight",
                            {
                                "source_id": "catalog-test",
                                "revision": "attacker-revision",
                            },
                        )
                    )
                    self.assertEqual("policy_denied", wrong_revision["error"]["code"])

                    start = _value(
                        await client.call_tool(
                            "scout_start",
                            {
                                "source_id": "catalog-test",
                                "revision": "revision-1",
                                "objective": "inspect the registered catalog fixture",
                            },
                        )
                    )
                    session_id = start["session_id"]
                    version = start["version"]
                    manifests = []
                    for fixture_id in ("catalog-home", "catalog-page-2"):
                        explored = _value(
                            await client.call_tool(
                                "scout_explore_fixture",
                                {
                                    "session_id": session_id,
                                    "expected_version": version,
                                    "fixture_id": fixture_id,
                                },
                            )
                        )
                        version = explored["version"]
                        manifests.append(explored["manifest_id"])
                    finding = _value(
                        await client.call_tool(
                            "scout_record_finding",
                            {
                                "session_id": session_id,
                                "expected_version": version,
                                "statement": "The fixture catalog links to its second page.",
                                "epistemic_status": "observation",
                                "confidence": 1.0,
                                "manifest_ids": manifests,
                                "verification_note": "Observed in the retained fixture bytes.",
                            },
                        )
                    )
                    await client.call_tool(
                        "scout_propose",
                        {
                            "session_id": session_id,
                            "expected_version": finding["version"],
                            "summary": "Capture the fixture catalog pages.",
                            "finding_ids": finding["finding_ids"],
                            "manifest_ids": manifests,
                            "assumptions": [],
                            "risks": [
                                "Fixture replay does not establish production behavior."
                            ],
                            "required_capabilities": [
                                "http.fetch",
                                "links.discover",
                                "output.raw",
                            ],
                        },
                    )
                    transcript_result = _value(
                        await client.call_tool(
                            "scout_export_transcript", {"session_id": session_id}
                        )
                    )
                    transcript = json.loads(transcript_result["transcript_json"])
                    draft_yaml = dump_yaml(build_draft(transcript))
                    replay_result = _value(
                        await client.call_tool(
                            "scout_bc15_replay",
                            {"session_id": session_id, "draft_yaml": draft_yaml},
                        )
                    )
                    self.assertEqual("passed", replay_result["state"])
                    run_ids.append(replay_result["run_id"])
                    replay_report = replay_result["report"]
                    self.assertEqual("passed", replay_report["state"])
                    self.assertEqual(2, len(replay_report["raw_outputs"]))

                    accepted = _value(
                        await client.call_tool(
                            "scout_bc15_accept",
                            {"session_id": session_id, "draft_yaml": draft_yaml},
                        )
                    )
                    run_ids.append(accepted["run_id"])
                    self.assertEqual("passed", accepted["state"])
                    report = accepted["report"]
                    acceptance = report["acquisition_acceptance"]
                    self.assertEqual("accepted", acceptance["state"])
                    self.assertTrue(acceptance["raw_output_only"])
                    self.assertFalse(acceptance["records_extracted"])
                    self.assertEqual(
                        2, len(report["replay_execution"]["replayed_pages"])
                    )
                    self.assertEqual(2, len(report["replay_execution"]["raw_outputs"]))

                    run_id = accepted["run_id"]
                    status = _value(
                        await client.call_tool(
                            "scout_bc15_status",
                            {"session_id": session_id, "run_id": run_id},
                        )
                    )
                    self.assertEqual("passed", status["state"])
                    manifest = _value(
                        await client.call_tool(
                            "scout_bc15_get_manifest",
                            {"session_id": session_id, "run_id": run_id},
                        )
                    )
                    self.assertTrue(manifest["manifest"]["complete"])
                    report_result = _value(
                        await client.call_tool(
                            "scout_bc15_get_report",
                            {"session_id": session_id, "run_id": run_id},
                        )
                    )
                    self.assertEqual(report, report_result["report"])
                    all_results = json.dumps(
                        [replay_result, accepted, status, manifest, report_result]
                    )
                    self.assertNotIn('data-id="r-101"', all_results)
                    self.assertNotIn('data-id="r-102"', all_results)
            finally:
                await http_client.aclose()
                if process.returncode is None:
                    process.terminate()
                    await process.wait()

        with tempfile.TemporaryDirectory() as temporary:
            registry_path = Path(temporary) / "bc15-runs.sqlite3"
            asyncio.run(exercise(Path(temporary) / "custody"))
            from scout.tool_host.protected_run_registry import ProtectedRunRegistry

            accepted_run = ProtectedRunRegistry(registry_path).get(run_ids[-1])
            self.assertEqual("passed", accepted_run["state"])
            self.assertEqual("local-analyst", accepted_run["principal"])
            self.assertEqual(
                "accepted", accepted_run["report"]["acquisition_acceptance"]["state"]
            )


if __name__ == "__main__":
    unittest.main()
