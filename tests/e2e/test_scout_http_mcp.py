"""Real MCP host, PostgreSQL, Squid, TLS fixture, custody, and replay test."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

import aiohttp
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from psycopg import connect
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_IP = os.environ.get("SCOUT_HTTP_E2E_FIXTURE_IP", "172.30.91.10")
SINK_IP = os.environ.get("SCOUT_HTTP_E2E_SINK_IP", "172.30.91.11")
sys.path.insert(0, str(ROOT / "apps" / "scout"))

from scout.adapters.local_custody import LocalFileEvidenceCustody
from scout.adapters.postgres.repository import PostgresScoutSessionRepository
from scout.tools.transport import AiohttpSquidTransport


@unittest.skipUnless(
    os.environ.get("SCOUT_HTTP_E2E") == "1",
    "run through make scout-http-e2e to start the isolated Docker profile",
)
class ScoutHTTPMcpE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dsn = os.environ["SCOUT_HTTP_E2E_DATABASE_URL"]
        if urlparse(cls.base_dsn).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("HTTP e2e PostgreSQL DSN must bind to loopback")
        cls.database = "scout_http_e2e_" + uuid4().hex
        with connect(cls.base_dsn, autocommit=True) as conn:
            conn.execute(SQL("CREATE DATABASE {}").format(Identifier(cls.database)))
        cls.dsn = make_conninfo(cls.base_dsn, dbname=cls.database)
        cls.repository = PostgresScoutSessionRepository(cls.dsn)
        cls.repository.migrate()

    @classmethod
    def tearDownClass(cls):
        with connect(cls.base_dsn, autocommit=True) as conn:
            conn.execute(
                SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    Identifier(cls.database)
                )
            )

    @staticmethod
    def configure_dns(zones):
        request = Request(
            os.environ["SCOUT_HTTP_E2E_DNS_CONTROL_URL"] + "/configure",
            data=json.dumps({"zones": zones}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise AssertionError("controlled DNS configuration failed")

    @staticmethod
    def dns_state():
        with urlopen(
            os.environ["SCOUT_HTTP_E2E_DNS_CONTROL_URL"] + "/state", timeout=3
        ) as response:
            return json.load(response)

    @staticmethod
    def has_dns_query(queries, name, qtype):
        return any(
            query["name"] == name and query["type"] == qtype for query in queries
        )

    @staticmethod
    async def run_cli(*arguments, env):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "collector_engineering",
            *arguments,
            cwd=ROOT,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(), stderr.decode()

    def test_authorized_https_mcp_custody_transcript_and_restart_replay(self):
        async def scenario():
            from scout.domain import SourceRef

            source = SourceRef("e2e-catalog", "revision-7")
            raw_source = SourceRef("controlled-http", "controlled-raw-pdf-v1")
            parameter_source = SourceRef("controlled-http", "controlled-work-intake-v1")
            structured_source = parameter_source
            start_url = "https://catalog.fixture.test/docs/page"
            self.configure_dns(
                {
                    "catalog.fixture.test": {
                        "A": [FIXTURE_IP],
                        "ttl": 60,
                    },
                    "api.fixture.test": {
                        "A": [FIXTURE_IP],
                        "ttl": 60,
                    },
                }
            )
            policy_path = Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / "policy.json"
            custody_root = Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / "custody"
            principal = "e2e-analyst"
            policy = {
                "version": 1,
                "sources": [
                    {
                        "source_id": source.source_id,
                        "revision": source.revision,
                        "grants": [
                            {
                                "origin": "https://catalog.fixture.test",
                                "path_prefixes": ["/docs"],
                            }
                        ],
                        "limits": {
                            "max_requests": 4,
                            "max_duration_seconds": 60,
                            "max_response_bytes": 32768,
                        },
                    },
                    {
                        "source_id": raw_source.source_id,
                        "revision": raw_source.revision,
                        "grants": [
                            {
                                "origin": "https://api.fixture.test",
                                "path_prefixes": ["/v1/artifacts/pdf"],
                            }
                        ],
                        "limits": {
                            "max_requests": 1,
                            "max_duration_seconds": 60,
                            "max_response_bytes": 2_000_000,
                        },
                    },
                    *[
                        {
                            "source_id": item.source_id,
                            "revision": item.revision,
                            "grants": [
                                {
                                    "origin": "https://api.fixture.test",
                                    "path_prefixes": ["/v1/items"],
                                }
                            ],
                            "limits": {
                                "max_requests": 1,
                                "max_duration_seconds": 60,
                                "max_response_bytes": 32768,
                            },
                        }
                        for item in dict.fromkeys((structured_source, parameter_source))
                    ],
                ],
                "approvals": [
                    {
                        "principal": principal,
                        "source_id": source.source_id,
                        "revision": source.revision,
                        "grants": [
                            {
                                "origin": "https://catalog.fixture.test",
                                "path_prefixes": ["/docs"],
                            }
                        ],
                        "expires_at": (
                            datetime.now(UTC) + timedelta(hours=1)
                        ).isoformat(),
                        "limits": {
                            "max_requests": 4,
                            "max_duration_seconds": 60,
                            "max_response_bytes": 32768,
                        },
                    },
                    {
                        "principal": "e2e-other-tenant",
                        "source_id": source.source_id,
                        "revision": source.revision,
                        "grants": [
                            {
                                "origin": "https://catalog.fixture.test",
                                "path_prefixes": ["/docs"],
                            }
                        ],
                        "expires_at": (
                            datetime.now(UTC) + timedelta(hours=1)
                        ).isoformat(),
                        "limits": {
                            "max_requests": 4,
                            "max_duration_seconds": 60,
                            "max_response_bytes": 32768,
                        },
                    },
                    {
                        "principal": principal,
                        "source_id": raw_source.source_id,
                        "revision": raw_source.revision,
                        "grants": [
                            {
                                "origin": "https://api.fixture.test",
                                "path_prefixes": ["/v1/artifacts/pdf"],
                            }
                        ],
                        "expires_at": (
                            datetime.now(UTC) + timedelta(hours=1)
                        ).isoformat(),
                        "limits": {
                            "max_requests": 1,
                            "max_duration_seconds": 60,
                            "max_response_bytes": 2_000_000,
                        },
                    },
                    *[
                        {
                            "principal": principal,
                            "source_id": item.source_id,
                            "revision": item.revision,
                            "grants": [
                                {
                                    "origin": "https://api.fixture.test",
                                    "path_prefixes": ["/v1/items"],
                                }
                            ],
                            "expires_at": (
                                datetime.now(UTC) + timedelta(hours=1)
                            ).isoformat(),
                            "limits": {
                                "max_requests": 1,
                                "max_duration_seconds": 60,
                                "max_response_bytes": 32768,
                            },
                        }
                        for item in dict.fromkeys((structured_source, parameter_source))
                    ],
                ],
            }
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            policy_path.chmod(0o600)

            env = os.environ.copy()
            env.update(
                {
                    "PYTHONPATH": str(ROOT / "apps" / "scout"),
                    "SCOUT_DATABASE_URL": self.dsn,
                    "SCOUT_LOCAL_PRINCIPAL": principal,
                    "SCOUT_HOST_POLICY_FILE": str(policy_path),
                    "SCOUT_EVIDENCE_ROOT": str(custody_root),
                    "SCOUT_ARTIFACT_ROOT": str(
                        Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / "review-artifacts"
                    ),
                    "SCOUT_EGRESS_PROXY": os.environ["SCOUT_HTTP_E2E_PROXY_URL"],
                    "SSL_CERT_FILE": os.environ["SCOUT_HTTP_E2E_CA_FILE"],
                }
            )
            candidate_dir = None
            candidate_dir_value = os.environ.get("SCOUT_HTTP_E2E_CANDIDATE_DIR")
            if candidate_dir_value:
                candidate_dir = Path(candidate_dir_value)
                if not candidate_dir.is_absolute() or candidate_dir.is_symlink():
                    raise ValueError("candidate output directory is unsafe")
                candidate_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "tests" / "mcp_authoring_stdio_fixture.py")],
                env=env,
            )
            source_args = {
                "source_id": source.source_id,
                "revision": source.revision,
                "objective": "verify controlled HTTPS discovery and replay",
            }

            async def fetch_other_principal_manifest():
                foreign_env = env | {"SCOUT_LOCAL_PRINCIPAL": "e2e-other-tenant"}
                foreign_params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(ROOT / "tests" / "mcp_authoring_stdio_fixture.py")],
                    env=foreign_env,
                )
                async with (
                    stdio_client(foreign_params) as (foreign_read, foreign_write),
                    ClientSession(foreign_read, foreign_write) as foreign_client,
                ):
                    await foreign_client.initialize()
                    foreign_start = await foreign_client.call_tool(
                        "scout_start", source_args
                    )
                    foreign_start_value = json.loads(foreign_start.content[0].text)
                    self.assertTrue(foreign_start_value["ok"], foreign_start_value)
                    foreign_fetch = await foreign_client.call_tool(
                        "scout_fetch",
                        {
                            "session_id": foreign_start_value["session_id"],
                            "expected_version": 0,
                            "url": "https://catalog.fixture.test/docs/next",
                        },
                    )
                    foreign_fetch_value = json.loads(foreign_fetch.content[0].text)
                    self.assertTrue(foreign_fetch_value["ok"], foreign_fetch_value)
                    return foreign_fetch_value["manifest_id"]

            foreign_manifest_id = await fetch_other_principal_manifest()
            session_id = None
            fetch_args = None
            first = None
            transcript = None

            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()
                tools = {item.name: item for item in (await client.list_tools()).tools}
                self.assertTrue(
                    {
                        "scout_preflight",
                        "scout_start",
                        "scout_fetch",
                        "scout_record_finding",
                        "scout_propose",
                        "scout_export_transcript",
                        "scout_proposal_history",
                        "scout_validate_proposal",
                        "scout_prepare_promotion",
                        "scout_bc15_replay",
                        "scout_bc15_accept",
                    }
                    <= set(tools)
                )
                self.assertFalse(
                    {
                        "scout_promote_proposal",
                        "scout_approve_release",
                        "scout_sign_authority",
                        "scout_install_authority",
                    }
                    & set(tools)
                )
                self.assertIn("scout_fetch", tools)
                self.assertFalse(
                    tools["scout_fetch"].inputSchema["additionalProperties"]
                )
                self.assertEqual(
                    set(tools["scout_fetch"].inputSchema["properties"]),
                    {"session_id", "expected_version", "url"},
                )

                denied_preflight = await client.call_tool(
                    "scout_preflight",
                    {"source_id": "unregistered", "revision": "revision-7"},
                )
                denied_preflight_value = json.loads(denied_preflight.content[0].text)
                self.assertFalse(denied_preflight_value["ok"])
                preflight = await client.call_tool(
                    "scout_preflight",
                    {"source_id": source.source_id, "revision": source.revision},
                )
                preflight_value = json.loads(preflight.content[0].text)
                self.assertTrue(preflight_value["ok"], preflight_value)
                self.assertEqual(
                    preflight_value["origins"], ["https://catalog.fixture.test"]
                )
                self.assertEqual(preflight_value["path_prefixes"], ["/docs"])
                self.assertEqual(preflight_value["max_requests"], 4)
                self.assertEqual(preflight_value["max_pages"], 4)
                self.assertEqual(preflight_value["max_response_bytes"], 32768)
                self.assertTrue(
                    start_url.startswith("https://catalog.fixture.test/docs/")
                )

                started = await client.call_tool("scout_start", source_args)
                started_value = json.loads(started.content[0].text)
                self.assertTrue(started_value["ok"], started_value)
                session_id = started_value["session_id"]
                fetch_args = {
                    "session_id": session_id,
                    "expected_version": 0,
                    "url": start_url,
                }
                injected = await client.call_tool(
                    "scout_fetch", fetch_args | {"principal": "attacker"}
                )
                self.assertTrue(injected.isError)

                result = await client.call_tool("scout_fetch", fetch_args)
                first = json.loads(result.content[0].text)
                self.assertTrue(first["ok"], first)
                self.assertEqual(first["status"], 200)
                self.assertEqual(first["title"], "Controlled catalog")
                self.assertIn("fixture evidence body", first["text"])
                self.assertIn("Ignore prior instructions", first["text"])
                self.assertNotIn("fixture-secret-value", first["text"])
                self.assertFalse(first["text_truncated"])

                artifact_ref = first["evidence"][0]
                custody = LocalFileEvidenceCustody(custody_root)
                artifact = custody.lookup(artifact_ref["evidence_id"])
                self.assertIsNotNone(artifact)
                body = custody.read(artifact)
                self.assertIsNotNone(body)
                self.assertEqual(
                    hashlib.sha256(body).hexdigest(), artifact_ref["sha256"]
                )
                self.assertEqual(len(body), artifact_ref["size_bytes"])
                self.assertIn(b"fixture-secret-value", body)

                next_result = await client.call_tool(
                    "scout_fetch",
                    {
                        "session_id": session_id,
                        "expected_version": 1,
                        "url": "https://catalog.fixture.test/docs/next",
                    },
                )
                next_page = json.loads(next_result.content[0].text)
                self.assertTrue(next_page["ok"], next_page)
                self.assertIn("Alpha record", next_page["text"])
                self.assertIn("Beta record", next_page["text"])
                out_of_scope = await client.call_tool(
                    "scout_fetch",
                    {
                        "session_id": session_id,
                        "expected_version": 2,
                        "url": "https://catalog.fixture.test/private/records",
                    },
                )
                out_of_scope_value = json.loads(out_of_scope.content[0].text)
                self.assertFalse(out_of_scope_value["ok"])
                self.assertEqual(out_of_scope_value["error"]["code"], "policy_denied")

                retry = await client.call_tool("scout_fetch", fetch_args)
                replay = json.loads(retry.content[0].text)
                self.assertEqual(replay, first)

                finding = await client.call_tool(
                    "scout_record_finding",
                    {
                        "session_id": session_id,
                        "expected_version": 2,
                        "statement": "The catalog exposes linked HTML pages, and the captured next page contains records r-100 and r-101.",
                        "epistemic_status": "observation",
                        "confidence": 1.0,
                        "manifest_ids": [
                            first["manifest_id"],
                            next_page["manifest_id"],
                        ],
                        "verification_note": "The listing link and both record labels were observed in custody-backed HTTPS response text.",
                    },
                )
                finding_value = json.loads(finding.content[0].text)
                self.assertTrue(finding_value["ok"], finding_value)
                cross_principal_evidence = await client.call_tool(
                    "scout_propose",
                    {
                        "session_id": session_id,
                        "expected_version": 3,
                        "summary": "Attempt to reference evidence owned by another Scout principal.",
                        "finding_ids": finding_value["finding_ids"],
                        "manifest_ids": [foreign_manifest_id],
                        "assumptions": [],
                        "risks": [],
                        "required_capabilities": ["http.fetch", "links.discover"],
                    },
                )
                cross_principal_value = json.loads(
                    cross_principal_evidence.content[0].text
                )
                self.assertFalse(cross_principal_value["ok"])
                self.assertEqual(
                    cross_principal_value["error"]["code"], "invalid_command"
                )
                proposal = await client.call_tool(
                    "scout_propose",
                    {
                        "session_id": session_id,
                        "expected_version": 3,
                        "summary": "Fetch the catalog start page, discover its same-scope next-page link, and retain bounded raw listing responses for offline review.",
                        "finding_ids": [finding_value["finding_ids"][-1]],
                        "manifest_ids": [
                            first["manifest_id"],
                            next_page["manifest_id"],
                        ],
                        "assumptions": [
                            "The observed listing link represents the next bounded page in this controlled fixture."
                        ],
                        "risks": [
                            "Pagination conventions beyond the captured page remain unverified.",
                            "Browser-rendered JavaScript and production BC01/BC06/BC11 adapters are outside this test.",
                        ],
                        "required_capabilities": [
                            "http.fetch",
                            "links.discover",
                            "pagination.bounded",
                            "output.raw",
                        ],
                    },
                )
                self.assertTrue(json.loads(proposal.content[0].text)["ok"])
                exported = await client.call_tool(
                    "scout_export_transcript", {"session_id": session_id}
                )
                exported_value = json.loads(exported.content[0].text)
                self.assertTrue(exported_value["ok"], exported_value)
                transcript = json.loads(exported_value["transcript_json"])
                self.assertEqual(
                    "scout-transcript-draft/v3", transcript["transcript_schema"]
                )
                self.assertEqual(
                    start_url, transcript["navigation"]["start_requested_url"]
                )
                self.assertEqual(
                    [1, 2], [page["sequence"] for page in transcript["pages"]]
                )
                self.assertEqual(
                    ["text/html", "text/html"],
                    [page["content_type"] for page in transcript["pages"]],
                )
                self.assertFalse(transcript["redaction"]["raw_content_included"])
                self.assertEqual(
                    transcript["session"]["source"],
                    {
                        "source_id": source.source_id,
                        "revision": source.revision,
                    },
                )
                self.assertEqual(
                    transcript["pages"][0]["sha256"], artifact_ref["sha256"]
                )
                self.assertNotIn(
                    "fixture-secret-value", exported_value["transcript_json"]
                )
                transcript_sha256 = exported_value["sha256"]
                self.assertEqual(
                    hashlib.sha256(
                        exported_value["transcript_json"].encode("utf-8")
                    ).hexdigest(),
                    transcript_sha256,
                )
                draft_input = await asyncio.create_subprocess_exec(
                    str(ROOT / ".venv/bin/python"),
                    "-c",
                    "import json,sys; from collector_engineering.playbooks import build_draft,dump_yaml; print(dump_yaml(build_draft(json.load(sys.stdin))))",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                draft_stdout, draft_stderr = await draft_input.communicate(
                    exported_value["transcript_json"].encode("utf-8")
                )
                self.assertEqual(draft_input.returncode, 0, draft_stderr.decode())
                protected_draft = draft_stdout.decode("utf-8")
                replay_call = await client.call_tool(
                    "scout_bc15_replay",
                    {"session_id": session_id, "draft_yaml": protected_draft},
                )
                replay_call_value = json.loads(replay_call.content[0].text)
                self.assertTrue(replay_call_value["ok"], replay_call_value)
                self.assertEqual("passed", replay_call_value["state"])
                replay_report = replay_call_value["report"]
                self.assertEqual(2, len(replay_report["replayed_pages"]))
                self.assertNotIn("fixture-secret-value", replay_call.content[0].text)
                accept_call = await client.call_tool(
                    "scout_bc15_accept",
                    {"session_id": session_id, "draft_yaml": protected_draft},
                )
                accept_call_value = json.loads(accept_call.content[0].text)
                self.assertTrue(accept_call_value["ok"], accept_call_value)
                acceptance_report = accept_call_value["report"]
                self.assertEqual(
                    "accepted",
                    acceptance_report["acquisition_acceptance"]["state"],
                )

            persisted = self.repository.get_session(session_id)
            self.assertEqual(persisted.principal, principal)
            self.assertEqual(persisted.source, source)
            self.assertEqual(len(persisted.observations), 2)
            self.assertEqual(
                [1, 2],
                [item.exploration_sequence for item in persisted.observations],
            )
            self.assertEqual(start_url, persisted.start_requested_url)
            self.assertEqual(persisted.remaining_requests, 2)
            self.assertEqual(
                persisted.observations[0].extraction_version, "scout-text-v1"
            )
            with connect(self.dsn) as conn:
                budget = conn.execute(
                    "SELECT requests_used FROM scout_navigation_budgets WHERE session_id=%s",
                    (session_id,),
                ).fetchone()
                request_count = conn.execute(
                    "SELECT count(*) FROM scout_navigation_budget_requests WHERE session_id=%s",
                    (session_id,),
                ).fetchone()[0]
            self.assertEqual(budget, (2,))
            self.assertEqual(request_count, 2)

            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as restarted_client,
            ):
                await restarted_client.initialize()
                after_restart = await restarted_client.call_tool(
                    "scout_fetch", fetch_args
                )
                restarted_value = json.loads(after_restart.content[0].text)
                self.assertEqual(restarted_value, first)

                # Exercise the current BC14→BC15 raw-only authoring handoff
                # with one controlled PDF observation. The response body is
                # held by local custody; the proposal contains only metadata.
                pdf_start = await restarted_client.call_tool(
                    "scout_start",
                    {
                        "source_id": raw_source.source_id,
                        "revision": raw_source.revision,
                        "objective": "identify the controlled downloadable PDF",
                    },
                )
                pdf_started = json.loads(pdf_start.content[0].text)
                self.assertTrue(pdf_started["ok"], pdf_started)
                pdf_session = pdf_started["session_id"]
                pdf_fetch = await restarted_client.call_tool(
                    "scout_fetch",
                    {
                        "session_id": pdf_session,
                        "expected_version": 0,
                        "url": "https://api.fixture.test/v1/artifacts/pdf",
                    },
                )
                pdf_value = json.loads(pdf_fetch.content[0].text)
                self.assertTrue(pdf_value["ok"], pdf_value)
                self.assertEqual("application/pdf", pdf_value["content_type"])
                self.assertEqual("", pdf_value["text"])
                pdf_evidence = pdf_value["evidence"][0]
                pdf_artifact = custody.lookup(pdf_evidence["evidence_id"])
                pdf_bytes = custody.read(pdf_artifact)
                self.assertEqual(
                    hashlib.sha256(pdf_bytes).hexdigest(), pdf_evidence["sha256"]
                )
                self.assertEqual(len(pdf_bytes), pdf_evidence["size_bytes"])
                self.assertEqual(
                    pdf_bytes,
                    (
                        ROOT / "examples/use-cases/raw-artifact/expected/sample.pdf"
                    ).read_bytes(),
                )
                finding = await restarted_client.call_tool(
                    "scout_record_finding",
                    {
                        "session_id": pdf_session,
                        "expected_version": 1,
                        "statement": "The authorized document endpoint declares application/pdf and returns one custody-verified opaque response.",
                        "epistemic_status": "observation",
                        "confidence": 1.0,
                        "manifest_ids": [pdf_value["manifest_id"]],
                        "verification_note": "The response media declaration, size and custody digest were observed; the PDF contents were not parsed.",
                    },
                )
                finding_value = json.loads(finding.content[0].text)
                self.assertTrue(finding_value["ok"], finding_value)
                proposal = await restarted_client.call_tool(
                    "scout_propose",
                    {
                        "session_id": pdf_session,
                        "expected_version": 2,
                        "summary": "Acquire the single observed PDF response as an opaque raw artifact.",
                        "finding_ids": [finding_value["finding_ids"][-1]],
                        "manifest_ids": [pdf_value["manifest_id"]],
                        "assumptions": [],
                        "risks": [
                            "The declared media type is not semantic PDF validation."
                        ],
                        "required_capabilities": ["http.fetch", "output.raw"],
                    },
                )
                self.assertTrue(json.loads(proposal.content[0].text)["ok"])
                pdf_export = await restarted_client.call_tool(
                    "scout_export_transcript", {"session_id": pdf_session}
                )
                pdf_exported = json.loads(pdf_export.content[0].text)
                self.assertTrue(pdf_exported["ok"], pdf_exported)
                pdf_transcript = json.loads(pdf_exported["transcript_json"])
                self.assertEqual(
                    "scout-transcript-draft/v3", pdf_transcript["transcript_schema"]
                )
                self.assertEqual(
                    "application/pdf", pdf_transcript["pages"][0]["content_type"]
                )
                self.assertFalse(pdf_transcript["redaction"]["raw_content_included"])
                pdf_dir = Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / "pdf-authoring"
                pdf_dir.mkdir(mode=0o700)
                pdf_transcript_path = pdf_dir / "transcript.json"
                pdf_transcript_path.write_text(
                    pdf_exported["transcript_json"], encoding="utf-8"
                )
                draft_code, pdf_yaml, draft_error = await self.run_cli(
                    "draft",
                    str(pdf_transcript_path),
                    "--profile",
                    "raw-artifact-v4",
                    "--output",
                    str(pdf_dir / "capture-playbook.yaml"),
                    env=env,
                )
                self.assertEqual(0, draft_code, draft_error)
                self.assertIn("wrote draft", pdf_yaml)
                generated_pdf_draft = (pdf_dir / "capture-playbook.yaml").read_text()
                self.assertIn("capture-playbook-draft/v4", generated_pdf_draft)
                self.assertIn("application/pdf", generated_pdf_draft)
                candidate_specification = yaml.safe_load(generated_pdf_draft)
                revised_proposal = await restarted_client.call_tool(
                    "scout_propose",
                    {
                        "session_id": pdf_session,
                        "expected_version": 3,
                        "summary": "Acquire the single observed PDF response as an opaque raw artifact.",
                        "finding_ids": [finding_value["finding_ids"][-1]],
                        "manifest_ids": [pdf_value["manifest_id"]],
                        "assumptions": [],
                        "risks": [
                            "The declared media type is not semantic PDF validation."
                        ],
                        "required_capabilities": ["http.fetch", "output.raw"],
                        "candidate_specification": candidate_specification,
                    },
                )
                revised_value = json.loads(revised_proposal.content[0].text)
                self.assertTrue(revised_value["ok"], revised_value)
                self.assertEqual(2, revised_value["proposal_revision"])
                pdf_export = await restarted_client.call_tool(
                    "scout_export_transcript", {"session_id": pdf_session}
                )
                pdf_exported = json.loads(pdf_export.content[0].text)
                pdf_transcript = json.loads(pdf_exported["transcript_json"])
                self.assertEqual(
                    "scout-transcript-draft/v4", pdf_transcript["transcript_schema"]
                )
                self.assertEqual(2, pdf_transcript["proposal"]["proposal_revision"])
                self.assertEqual(
                    [1, 2],
                    [item["revision"] for item in pdf_transcript["proposal_history"]],
                )
                persisted_pdf_session = self.repository.get_session(pdf_session)
                self.assertEqual(
                    [1, 2],
                    [
                        item.proposal_revision
                        for item in persisted_pdf_session.proposal_history
                    ],
                )
                with connect(self.dsn) as conn:
                    proposal_events = conn.execute(
                        "SELECT proposal_revision FROM scout_outbox WHERE aggregate_id=%s ORDER BY proposal_revision",
                        (pdf_session,),
                    ).fetchall()
                self.assertEqual([(1,), (2,)], proposal_events)
                self.assertEqual(
                    candidate_specification,
                    pdf_transcript["proposal"]["candidate_specification"],
                )
                history_call = await restarted_client.call_tool(
                    "scout_proposal_history", {"session_id": pdf_session}
                )
                history_value = json.loads(history_call.content[0].text)
                self.assertEqual(
                    [1, 2], [item["revision"] for item in history_value["history"]]
                )
                validation_call = await restarted_client.call_tool(
                    "scout_validate_proposal", {"session_id": pdf_session}
                )
                validation_value = json.loads(validation_call.content[0].text)
                self.assertTrue(validation_value["valid"], validation_value)
                self.assertEqual(2, validation_value["proposal_revision"])
                handoff_call = await restarted_client.call_tool(
                    "scout_prepare_promotion", {"session_id": pdf_session}
                )
                handoff_value = json.loads(handoff_call.content[0].text)
                self.assertTrue(handoff_value["valid"], handoff_value)
                self.assertTrue(handoff_value["review_required"])
                self.assertFalse(handoff_value["promotion_authority_exposed"])
                self.assertNotIn("scout_promote_proposal", tools)
                from acquisition_lifecycle.adapters.postgres import (
                    PostgresLifecycleStore,
                )
                from acquisition_lifecycle.application import AcquisitionLifecycle
                from acquisition_lifecycle.domain import canonical_digest
                from collector_engineering.playbook_v2 import validate_playbook_v2
                from mozaic_contract_resolution import ImmutableContractRegistry
                from scout.application import ScoutApplication
                from scout.domain import AuthorizationError, IntegrityError, SourceRef
                from scout.proposal_workflow import (
                    ProposalDiagnostic,
                    ProposalWorkflow,
                )
                from scout.reviewer_identity import (
                    AuthenticatedReviewerAccess,
                    AuthenticatedReviewerPromotion,
                    ReviewerPromotionAdapter,
                    UnixReviewerIdentityProvider,
                )
                from scout.tool_host.composition import (
                    _LocalPolicyDecision,
                    _LocalSourceLookup,
                )
                from scout.tools.policy import LocalAuthorizationStore

                class UnusedPort:
                    def __getattr__(self, _name):
                        raise AssertionError("unexpected release approval operation")

                lifecycle_store = PostgresLifecycleStore(self.dsn, ROOT / "migrations")
                lifecycle_store.migrate()
                source_document = {
                    "contract_version": "source-revision/v1",
                    "source_id": raw_source.source_id,
                    "revision_id": raw_source.revision,
                    "owner": "controlled Scout E2E",
                    "status": "eligible",
                    "scope": {
                        "origins": ["https://api.fixture.test"],
                        "methods": ["GET"],
                        "path_prefixes": ["/v1/artifacts/pdf"],
                        "query_parameter_values": {},
                    },
                    "configuration_version": "1.0.0",
                    "configuration_digest": "",
                    "metadata": {"classification": "controlled-test-only"},
                }
                source_document["configuration_digest"] = canonical_digest(
                    {
                        key: value
                        for key, value in source_document.items()
                        if key != "configuration_digest"
                    }
                )
                lifecycle = AcquisitionLifecycle(
                    lifecycle_store,
                    ImmutableContractRegistry(ROOT / "contracts/payload/draft/v1"),
                    UnusedPort(),
                    UnusedPort(),
                    {},
                    UnusedPort(),
                    authority_id="scout-e2e-test-authority",
                )
                lifecycle.register_source_revision(
                    source_document, actor_id="controlled-test-operator"
                )
                for additional_source in dict.fromkeys(
                    (structured_source, parameter_source)
                ):
                    document = {
                        **source_document,
                        "source_id": additional_source.source_id,
                        "revision_id": additional_source.revision,
                        "scope": {
                            **source_document["scope"],
                            "path_prefixes": ["/v1/items"],
                        },
                    }
                    document["configuration_digest"] = canonical_digest(
                        {
                            key: value
                            for key, value in document.items()
                            if key != "configuration_digest"
                        }
                    )
                    lifecycle.register_source_revision(
                        document, actor_id="controlled-test-operator"
                    )

                reviewer_policy_path = (
                    Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / "reviewer-identities.json"
                )
                reviewer_policy_path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "agent_uid": 7001,
                            "reviewers": [
                                {
                                    "uid": 7002,
                                    "principal": "controlled-test-reviewer",
                                    "source_scopes": [
                                        {
                                            "source_id": item.source_id,
                                            "revision": item.revision,
                                        }
                                        for item in dict.fromkeys(
                                            (
                                                raw_source,
                                                structured_source,
                                                parameter_source,
                                            )
                                        )
                                    ],
                                }
                            ],
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                reviewer_policy_path.chmod(0o440)
                reviewer_identity_provider = UnixReviewerIdentityProvider(
                    reviewer_policy_path,
                    uid_provider=lambda: (7002, 7002),
                    expected_owner_uid=os.geteuid(),
                )
                review_identity = reviewer_identity_provider.current()
                source_policy = LocalAuthorizationStore.from_file(policy_path)
                reviewer_scout = ScoutApplication(
                    _LocalSourceLookup(source_policy),
                    _LocalPolicyDecision(source_policy),
                    self.repository,
                    LocalFileEvidenceCustody(custody_root),
                    proposal_review_access=AuthenticatedReviewerAccess(review_identity),
                )

                class CandidateValidator:
                    def validate(_self, candidate):
                        result = validate_playbook_v2(candidate)
                        return tuple(
                            ProposalDiagnostic(item.code, item.path, item.message)
                            for item in result.diagnostics
                        )

                class PlaybookPort:
                    def register(_self, candidate, *, actor_id):
                        registered = lifecycle.register_playbook_document(
                            candidate, actor_id=actor_id
                        )
                        return registered

                async def author_structured_candidate(
                    source_ref, url, template_path, output_name, required_capabilities
                ):
                    started = await restarted_client.call_tool(
                        "scout_start",
                        {
                            "source_id": source_ref.source_id,
                            "revision": source_ref.revision,
                            "objective": (
                                "observe the controlled JSON response for the "
                                + output_name
                                + " declarative acquisition proposal"
                            ),
                        },
                    )
                    started_value = json.loads(started.content[0].text)
                    self.assertTrue(started_value["ok"], started_value)
                    candidate_session = started_value["session_id"]
                    fetched = await restarted_client.call_tool(
                        "scout_fetch",
                        {
                            "session_id": candidate_session,
                            "expected_version": 0,
                            "url": url,
                        },
                    )
                    fetched_value = json.loads(fetched.content[0].text)
                    self.assertTrue(fetched_value["ok"], fetched_value)
                    finding_call = await restarted_client.call_tool(
                        "scout_record_finding",
                        {
                            "session_id": candidate_session,
                            "expected_version": 1,
                            "statement": "The controlled JSON response exposes the observed item fields and values relevant to this request.",
                            "epistemic_status": "observation",
                            "confidence": 1.0,
                            "manifest_ids": [fetched_value["manifest_id"]],
                            "verification_note": "The finding is bound to the digest-verified controlled HTTPS response.",
                        },
                    )
                    finding_data = json.loads(finding_call.content[0].text)
                    self.assertTrue(finding_data["ok"], finding_data)
                    finding_id = finding_data["finding_ids"][-1]
                    initial = await restarted_client.call_tool(
                        "scout_propose",
                        {
                            "session_id": candidate_session,
                            "expected_version": 2,
                            "summary": "Extract only the supported JSON fields observed in the controlled response.",
                            "finding_ids": [finding_id],
                            "manifest_ids": [fetched_value["manifest_id"]],
                            "assumptions": [],
                            "risks": [
                                "Controlled fixture evidence does not establish production source behavior."
                            ],
                            "required_capabilities": required_capabilities,
                        },
                    )
                    initial_value = json.loads(initial.content[0].text)
                    self.assertTrue(initial_value["ok"], initial_value)
                    exported = await restarted_client.call_tool(
                        "scout_export_transcript", {"session_id": candidate_session}
                    )
                    exported_value = json.loads(exported.content[0].text)
                    self.assertTrue(exported_value["ok"], exported_value)
                    exported_transcript = json.loads(exported_value["transcript_json"])
                    evidence_ref = exported_transcript["pages"][0]["evidence_ref"]
                    candidate = yaml.safe_load(Path(template_path).read_text())
                    if "input.query.scalar" in required_capabilities:
                        candidate["dsl_version"] = "capture-playbook-draft/v3"
                        fetch = next(
                            operation
                            for operation in candidate["operations"]
                            if operation.get("op") == "http.fetch/v1"
                        )
                        fetch["url"] = url
                        fetch["input_query"] = {
                            "parameter": "limit",
                            "query_name": "limit",
                        }
                    candidate["source"]["revision"] = source_ref.revision
                    candidate["proposal_ref"] = initial_value["proposal_id"]
                    candidate["findings"] = [
                        {
                            "finding_id": finding_id,
                            "classification": "observed",
                            "evidence_refs": [evidence_ref],
                        }
                    ]
                    candidate["evidence"] = [
                        {
                            "evidence_ref": evidence_ref,
                            "sha256": exported_transcript["pages"][0]["sha256"],
                        }
                    ]
                    for operation in candidate["operations"]:
                        if "evidence_refs" in operation:
                            operation["evidence_refs"] = [evidence_ref]
                    revised = await restarted_client.call_tool(
                        "scout_propose",
                        {
                            "session_id": candidate_session,
                            "expected_version": 3,
                            "summary": "Extract only the supported JSON fields observed in the controlled response.",
                            "finding_ids": [finding_id],
                            "manifest_ids": [fetched_value["manifest_id"]],
                            "assumptions": [],
                            "risks": [
                                "Controlled fixture evidence does not establish production source behavior."
                            ],
                            "required_capabilities": required_capabilities,
                            "candidate_specification": candidate,
                        },
                    )
                    revised_value = json.loads(revised.content[0].text)
                    self.assertTrue(revised_value["ok"], revised_value)
                    exported = await restarted_client.call_tool(
                        "scout_export_transcript", {"session_id": candidate_session}
                    )
                    exported_value = json.loads(exported.content[0].text)
                    exported_transcript = json.loads(exported_value["transcript_json"])
                    candidate_path = (
                        Path(os.environ["SCOUT_HTTP_E2E_CANDIDATE_DIR"]) / output_name
                        if os.environ.get("SCOUT_HTTP_E2E_CANDIDATE_DIR")
                        else Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / output_name
                    )
                    candidate_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    candidate_path.write_text(
                        yaml.safe_dump(candidate), encoding="utf-8"
                    )
                    candidate_path.chmod(0o600)
                    validation_code, _, validation_error = await self.run_cli(
                        "validate", str(candidate_path), env=env
                    )
                    self.assertEqual(0, validation_code, validation_error)
                    validation = workflow.validate_for_review(
                        candidate_session,
                        review_identity.principal,
                        datetime.now(UTC),
                    )
                    self.assertTrue(validation.valid, validation.diagnostics)
                    promotion = reviewer.promote(
                        candidate_session,
                        validation.proposal_id,
                        validation.proposal_revision,
                        validation.candidate_sha256,
                        now=datetime.now(UTC),
                    )
                    self.assertEqual(
                        validation.candidate_sha256,
                        promotion["candidate_sha256"],
                    )
                    if os.environ.get("SCOUT_HTTP_E2E_CANDIDATE_DIR"):
                        (candidate_dir / f"{output_name}.provenance.json").write_text(
                            json.dumps(
                                {
                                    "source": exported_transcript["session"]["source"],
                                    "scout_request_id": candidate_session,
                                    "evidence_manifest_ids": [
                                        item["manifest_id"]
                                        for item in exported_transcript["evidence"]
                                    ],
                                    "finding_ids": [
                                        item["finding_id"]
                                        for item in exported_transcript["findings"]
                                    ],
                                    "proposal_id": validation.proposal_id,
                                    "proposal_revision": validation.proposal_revision,
                                    "candidate_sha256": validation.candidate_sha256,
                                    "promoted_playbook": promotion,
                                },
                                indent=2,
                                sort_keys=True,
                            )
                            + "\n",
                            encoding="utf-8",
                        )
                    return candidate_session, promotion

                workflow = ProposalWorkflow(
                    reviewer_scout,
                    CandidateValidator(),
                    AuthenticatedReviewerPromotion(review_identity),
                    PlaybookPort(),
                )

                reviewer = ReviewerPromotionAdapter(
                    reviewer_identity_provider, workflow
                )
                with self.assertRaises(AuthorizationError):
                    AuthenticatedReviewerAccess(review_identity).authorize(
                        review_identity.principal,
                        SourceRef("unapproved-source", "revision-1"),
                    )
                with self.assertRaises(AuthorizationError):
                    UnixReviewerIdentityProvider(
                        reviewer_policy_path,
                        uid_provider=lambda: (7001, 7001),
                        expected_owner_uid=os.geteuid(),
                    ).current()
                validation = workflow.validate_for_review(
                    pdf_session,
                    review_identity.principal,
                    datetime.now(UTC),
                )
                self.assertTrue(validation.valid)
                with self.assertRaisesRegex(IntegrityError, "promotion intent differs"):
                    reviewer.promote(
                        pdf_session,
                        validation.proposal_id,
                        validation.proposal_revision,
                        "0" * 64,
                        now=datetime.now(UTC),
                    )
                promoted = reviewer.promote(
                    pdf_session,
                    validation.proposal_id,
                    validation.proposal_revision,
                    validation.candidate_sha256,
                    now=datetime.now(UTC),
                )
                replayed_promotion = reviewer.promote(
                    pdf_session,
                    validation.proposal_id,
                    validation.proposal_revision,
                    validation.candidate_sha256,
                    now=datetime.now(UTC),
                )
                self.assertEqual(promoted, replayed_promotion)
                registered_playbook = lifecycle_store.resolve_playbook(
                    promoted["playbook_id"], promoted["playbook_digest"]
                )
                self.assertEqual(raw_source.source_id, registered_playbook["source_id"])
                self.assertEqual(
                    promoted["playbook_digest"],
                    "sha256:"
                    + hashlib.sha256(registered_playbook["content"]).hexdigest(),
                )
                self.assertEqual([], lifecycle_store.list_releases())
                self.assertEqual(
                    "sha256:" + validation.candidate_sha256,
                    promoted["playbook_digest"],
                )
                if os.environ.get("SCOUT_HTTP_E2E_CANDIDATE_DIR"):
                    (candidate_dir / "raw-provenance.json").write_text(
                        json.dumps(
                            {
                                "source": pdf_transcript["session"]["source"],
                                "scout_request_id": pdf_session,
                                "evidence_manifest_ids": [
                                    item["manifest_id"]
                                    for item in pdf_transcript["evidence"]
                                ],
                                "finding_ids": [
                                    item["finding_id"]
                                    for item in pdf_transcript["findings"]
                                ],
                                "proposal_id": validation.proposal_id,
                                "proposal_revision": validation.proposal_revision,
                                "candidate_sha256": validation.candidate_sha256,
                                "promoted_playbook": promoted,
                            },
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                (
                    structured_session,
                    structured_promotion,
                ) = await author_structured_candidate(
                    structured_source,
                    "https://api.fixture.test/v1/items?limit=2",
                    ROOT
                    / "examples/use-cases/controlled-work-intake/capture-playbook.yaml",
                    "structured.json.yaml",
                    [
                        "http.fetch",
                        "json.path_restricted",
                        "records.extract",
                        "output.records.jsonl",
                        "input.query.scalar",
                    ],
                )
                (
                    parameter_session,
                    parameter_promotion,
                ) = await author_structured_candidate(
                    parameter_source,
                    "https://api.fixture.test/v1/items?limit=1",
                    ROOT
                    / "examples/use-cases/controlled-work-intake/capture-playbook.yaml",
                    "parameterized.yaml",
                    [
                        "http.fetch",
                        "json.path_restricted",
                        "records.extract",
                        "output.records.jsonl",
                        "input.query.scalar",
                    ],
                )
                self.assertNotEqual(structured_session, parameter_session)
                self.assertNotEqual(
                    structured_promotion["playbook_digest"],
                    parameter_promotion["playbook_digest"],
                )
                fixture_root = pdf_dir / "empty-fixtures"
                fixture_root.mkdir(mode=0o700)
                build_root = pdf_dir / "verified-build"
                compile_code, compile_stdout, compile_stderr = await self.run_cli(
                    "compile",
                    str(pdf_dir / "capture-playbook.yaml"),
                    "--backend",
                    "scrapy-live-http",
                    "--fixtures",
                    str(fixture_root),
                    "--output",
                    str(build_root),
                    env=env,
                )
                self.assertEqual(0, compile_code, compile_stderr or compile_stdout)
                verify_code, verify_stdout, verify_stderr = await self.run_cli(
                    "verify-build", str(build_root), env=env
                )
                self.assertEqual(0, verify_code, verify_stderr or verify_stdout)
                pdf_transcript_path.write_text(
                    pdf_exported["transcript_json"], encoding="utf-8"
                )
                candidate_code, _, candidate_error = await self.run_cli(
                    "draft",
                    str(pdf_transcript_path),
                    "--output",
                    str(pdf_dir / "promoted-candidate.yaml"),
                    env=env,
                )
                self.assertEqual(0, candidate_code, candidate_error)
                validate_code, _, validate_error = await self.run_cli(
                    "validate", str(pdf_dir / "promoted-candidate.yaml"), env=env
                )
                self.assertEqual(0, validate_code, validate_error)
                validate_code, _, validate_error = await self.run_cli(
                    "validate", str(pdf_dir / "capture-playbook.yaml"), env=env
                )
                self.assertEqual(0, validate_code, validate_error)
                candidate_out = os.environ.get("SCOUT_HTTP_E2E_RAW_CANDIDATE_OUT")
                if candidate_out:
                    destination = Path(candidate_out)
                    self.assertTrue(destination.is_absolute())
                    self.assertFalse(destination.is_symlink())
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    destination.write_text(generated_pdf_draft, encoding="utf-8")
                    destination.chmod(0o600)
                if candidate_dir is not None:
                    raw_candidate = candidate_dir / "raw.yaml"
                    raw_candidate.write_text(generated_pdf_draft, encoding="utf-8")
                    raw_candidate.chmod(0o600)

            artifact_root = (
                Path(os.environ["SCOUT_HTTP_E2E_TEMP"])
                / "review-artifacts"
                / session_id
            )
            artifact_root.mkdir(parents=True, exist_ok=True)
            transcript_path = artifact_root / "transcript.json"
            transcript_path.write_text(
                exported_value["transcript_json"], encoding="utf-8"
            )
            (artifact_root / "transcript.sha256").write_text(
                transcript_sha256 + "\n", encoding="ascii"
            )
            (artifact_root / "session-metadata.json").write_text(
                json.dumps(
                    {
                        "session": transcript["session"],
                        "approved_scope": transcript["approved_scope"],
                        "producer": transcript["producer"],
                        "transcript_sha256": transcript_sha256,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            for filename, value in (
                ("evidence-manifest.json", transcript["evidence"]),
                ("findings.json", transcript["findings"]),
                ("playbook-proposal.json", transcript["proposal"]),
            ):
                (artifact_root / filename).write_text(
                    json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

            env["PYTHONPATH"] = os.pathsep.join(
                [
                    str(ROOT / "apps" / "collector-engineering"),
                    str(ROOT / "apps" / "scout"),
                ]
            )
            draft_path = artifact_root / "capture-playbook.yaml"
            draft_code, _, draft_stderr = await self.run_cli(
                "draft",
                str(transcript_path),
                "--output",
                str(draft_path),
                env=env,
            )
            self.assertEqual(draft_code, 0, draft_stderr)

            validation_code, validation_stdout, validation_stderr = await self.run_cli(
                "validate", str(draft_path), env=env
            )
            self.assertEqual(validation_code, 0, validation_stderr)
            (artifact_root / "validation-report.json").write_text(
                json.dumps(
                    {
                        "state": "passed",
                        "command": "collector_engineering validate capture-playbook.yaml",
                        "stdout": validation_stdout.strip(),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            replay_path = artifact_root / "replay-report.json"
            replay_path.write_text(json.dumps(replay_report), encoding="utf-8")
            self.assertEqual(replay_report["state"], "passed")
            self.assertEqual(len(replay_report["replayed_pages"]), 2)
            self.assertEqual(replay_report["failures"], [])
            acceptance_path = artifact_root / "acquisition-acceptance.json"
            acceptance_path.write_text(json.dumps(acceptance_report), encoding="utf-8")
            acceptance = acceptance_report
            self.assertEqual("passed", acceptance["structural_validation"]["state"])
            self.assertEqual("passed", acceptance["replay_execution"]["state"])
            self.assertEqual("accepted", acceptance["acquisition_acceptance"]["state"])
            self.assertFalse(acceptance["acquisition_acceptance"]["records_extracted"])
            (artifact_root / "review-summary.md").write_text(
                "\n".join(
                    [
                        "# Scout review summary",
                        "",
                        f"- Session: `{session_id}`",
                        f"- Source: `{source.source_id}` revision `{source.revision}`",
                        f"- Transcript SHA-256: `{transcript_sha256}`",
                        f"- Proposal: `{transcript['proposal']['proposal_id']}` (received by BC15; not approval)",
                        "- Findings: 1 observed finding tied to two custody evidence manifests.",
                        "- BC15 draft validation: passed.",
                        "- Offline fixture replay: passed for 2 captured HTTPS pages.",
                        "- Security: page prompt injection remained untrusted data; out-of-scope fetch was denied.",
                        "- Limits: deterministic MCP/container test, not a live Codex run; BC01/BC06/BC11 are local adapters; Codex workspace-write must deny Docker daemon access.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            for path in artifact_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(
                        b"fixture-secret-value", path.read_bytes(), path.name
                    )

            return session_id

        session_id = asyncio.run(scenario())
        self.assertTrue(session_id)

    def test_squid_resolves_pins_and_denies_adversarial_dns_answers(self):
        async def fetch(host):
            return await transport.get(
                f"https://{host}/dns/{host.split('.')[0]}", 32768, 3
            )

        proxy = os.environ["SCOUT_HTTP_E2E_PROXY_URL"]
        transport = AiohttpSquidTransport(proxy)

        self.configure_dns(
            {
                "single.fixture.test": {"A": [FIXTURE_IP], "ttl": 60},
            }
        )
        with mock.patch.dict(
            os.environ, {"SSL_CERT_FILE": os.environ["SCOUT_HTTP_E2E_CA_FILE"]}
        ):
            positive = asyncio.run(fetch("single.fixture.test"))
        self.assertEqual(positive.status, 200)
        questions = self.dns_state()["queries"]
        self.assertTrue(self.has_dns_query(questions, "single.fixture.test", "A"))

        self.configure_dns(
            {
                "cname.fixture.test": {
                    "CNAME": "cname-edge.fixture.test",
                    "ttl": 60,
                },
                "cname-edge.fixture.test": {
                    "A": [FIXTURE_IP],
                    "ttl": 60,
                },
            }
        )
        with mock.patch.dict(
            os.environ, {"SSL_CERT_FILE": os.environ["SCOUT_HTTP_E2E_CA_FILE"]}
        ):
            cname = asyncio.run(fetch("cname.fixture.test"))
        self.assertEqual(cname.status, 200)
        cname_questions = self.dns_state()["queries"]
        self.assertTrue(self.has_dns_query(cname_questions, "cname.fixture.test", "A"))

        cases = (
            (
                "private.fixture.test",
                {"private.fixture.test": {"A": [SINK_IP]}},
            ),
            (
                "mixed-a.fixture.test",
                {"mixed-a.fixture.test": {"A": [FIXTURE_IP, SINK_IP]}},
            ),
            (
                "mixed-aaaa.fixture.test",
                {
                    "mixed-aaaa.fixture.test": {
                        "A": [FIXTURE_IP],
                        "AAAA": ["::1"],
                    }
                },
            ),
            (
                "special.fixture.test",
                {
                    "special.fixture.test": {
                        "A": [FIXTURE_IP],
                        "AAAA": [f"::ffff:{SINK_IP}", "5f00::1"],
                    }
                },
            ),
            (
                "retry.fixture.test",
                {"retry.fixture.test": {"A": ["203.0.113.7", SINK_IP]}},
            ),
        )
        for host, zones in cases:
            with self.subTest(host=host):
                self.configure_dns(zones)
                with self.assertRaises(aiohttp.ClientHttpProxyError) as denied:
                    asyncio.run(fetch(host))
                self.assertEqual(denied.exception.status, 403)

        self.configure_dns(
            {
                "rebinding.fixture.test": {
                    "A": [FIXTURE_IP],
                    "ttl": 0,
                    "next": {"A": [SINK_IP], "ttl": 60},
                }
            }
        )
        with mock.patch.dict(
            os.environ, {"SSL_CERT_FILE": os.environ["SCOUT_HTTP_E2E_CA_FILE"]}
        ):
            rebound = asyncio.run(fetch("rebinding.fixture.test"))
        self.assertEqual(rebound.status, 200)
        rebound_queries = [
            query
            for query in self.dns_state()["queries"]
            if query["name"] == "rebinding.fixture.test" and query["type"] == "A"
        ]
        self.assertEqual(len(rebound_queries), 1)

        self.configure_dns(
            {"unavailable.fixture.test": {"rcode": 2, "A": [], "AAAA": []}}
        )
        with self.assertRaises(aiohttp.ClientError):
            asyncio.run(fetch("unavailable.fixture.test"))

        self.configure_dns({"dns-drop.fixture.test": {"drop": True}})
        with self.assertRaises((aiohttp.ClientError, TimeoutError)):
            asyncio.run(fetch("dns-drop.fixture.test"))
        self.assertTrue(
            self.has_dns_query(
                self.dns_state()["queries"], "dns-drop.fixture.test", "A"
            )
        )

    def test_mcp_dns_failure_then_forbidden_recovery_and_allowed_control(self):
        async def scenario():
            source_id = "dns-recovery-source"
            revision = "dns-recovery-revision-1"
            principal = "dns-recovery-reviewer"
            recovery_host = "dns-recovery.fixture.test"
            policy_path = Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / "dns-policy.json"
            custody_root = Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / "dns-custody"
            limits = {
                "max_requests": 4,
                "max_duration_seconds": 60,
                "max_response_bytes": 32768,
            }
            source_values = []
            approval_values = []
            for current_source, current_revision, hostname in (
                (source_id, revision, recovery_host),
                (
                    "dns-positive-control",
                    "dns-positive-revision-1",
                    "catalog.fixture.test",
                ),
            ):
                grant = {
                    "origin": f"https://{hostname}",
                    "path_prefixes": ["/docs"],
                }
                source_values.append(
                    {
                        "source_id": current_source,
                        "revision": current_revision,
                        "grants": [grant],
                        "limits": limits,
                    }
                )
                approval_values.append(
                    {
                        "principal": principal,
                        "source_id": current_source,
                        "revision": current_revision,
                        "grants": [grant],
                        "expires_at": (
                            datetime.now(UTC) + timedelta(hours=1)
                        ).isoformat(),
                        "limits": limits,
                    }
                )
            policy_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sources": source_values,
                        "approvals": approval_values,
                    }
                ),
                encoding="utf-8",
            )
            policy_path.chmod(0o600)
            env = os.environ.copy()
            env.update(
                {
                    "PYTHONPATH": str(ROOT / "apps" / "scout"),
                    "SCOUT_DATABASE_URL": self.dsn,
                    "SCOUT_LOCAL_PRINCIPAL": principal,
                    "SCOUT_HOST_POLICY_FILE": str(policy_path),
                    "SCOUT_EVIDENCE_ROOT": str(custody_root),
                    "SCOUT_EGRESS_PROXY": os.environ["SCOUT_HTTP_E2E_PROXY_URL"],
                    "SSL_CERT_FILE": os.environ["SCOUT_HTTP_E2E_CA_FILE"],
                }
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(ROOT / "tests" / "mcp_authoring_stdio_fixture.py")],
                env=env,
            )

            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as client,
            ):
                await client.initialize()

                async def new_session(current_source, current_revision, objective):
                    value = await client.call_tool(
                        "scout_start",
                        {
                            "source_id": current_source,
                            "revision": current_revision,
                            "objective": objective,
                        },
                    )
                    result = json.loads(value.content[0].text)
                    self.assertTrue(result["ok"], result)
                    return result["session_id"]

                self.configure_dns(
                    {
                        recovery_host: {
                            "rcode": 2,
                            "A": [],
                            "ttl": 0,
                            "next": {"A": [SINK_IP], "ttl": 0},
                        }
                    }
                )
                failure_session = await new_session(
                    source_id, revision, "DNS failure then controlled recovery"
                )
                failure_started_ns = time.time_ns()
                failure_result = await client.call_tool(
                    "scout_fetch",
                    {
                        "session_id": failure_session,
                        "expected_version": 0,
                        "url": f"https://{recovery_host}/docs/page",
                    },
                )
                failure_finished_ns = time.time_ns()
                failure_value = json.loads(failure_result.content[0].text)
                self.assertFalse(failure_value["ok"], failure_value)
                self.assertEqual(failure_value["error"]["code"], "policy_denied")
                first_operation_dns = self.dns_state()
                first_operation_responses = [
                    item
                    for item in first_operation_dns["responses"]
                    if item["name"] == recovery_host
                ]
                first_a = [
                    item for item in first_operation_responses if item["type"] == "A"
                ]
                self.assertTrue(first_a, first_operation_dns)
                self.assertEqual(first_a[0]["rcode"], 2, first_a)
                self.assertTrue(
                    any(
                        item["type"] == "A"
                        and item["rcode"] == 0
                        and item["ordinal"] > first_a[0]["ordinal"]
                        and any(
                            answer.get("value") == SINK_IP for answer in item["answers"]
                        )
                        for item in first_operation_responses
                    ),
                    first_operation_dns,
                )
                first_operation = {
                    "session_id": failure_session,
                    "started_ns": failure_started_ns,
                    "finished_ns": failure_finished_ns,
                    "mcp_result": {
                        "ok": failure_value["ok"],
                        "error": failure_value.get("error"),
                    },
                    "dns_queries": first_operation_dns["queries"],
                    "dns_responses": first_operation_responses,
                }

                self.configure_dns(
                    {
                        "catalog.fixture.test": {
                            "A": [FIXTURE_IP],
                            "ttl": 60,
                        }
                    }
                )
                positive_session = await new_session(
                    "dns-positive-control",
                    "dns-positive-revision-1",
                    "Independent authorized operation after DNS recovery",
                )
                positive_started_ns = time.time_ns()
                positive_result = await client.call_tool(
                    "scout_fetch",
                    {
                        "session_id": positive_session,
                        "expected_version": 0,
                        "url": "https://catalog.fixture.test/docs/page",
                    },
                )
                positive_finished_ns = time.time_ns()
                positive_value = json.loads(positive_result.content[0].text)
                self.assertTrue(positive_value["ok"], positive_value)
                self.assertEqual(positive_value["status"], 200)
                positive_dns = self.dns_state()

            evidence = {
                "recovery_host": recovery_host,
                "first_operation": first_operation,
                "positive_control": {
                    "session_id": positive_session,
                    "started_ns": positive_started_ns,
                    "finished_ns": positive_finished_ns,
                    "mcp_result": {
                        "ok": positive_value["ok"],
                        "status": positive_value["status"],
                    },
                    "dns_queries": positive_dns["queries"],
                    "dns_responses": positive_dns["responses"],
                },
            }
            evidence_path = (
                Path(os.environ["SCOUT_HTTP_E2E_TEMP"]) / "dns-recovery-evidence.json"
            )
            evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")

        asyncio.run(scenario())

    def test_proxy_denies_private_destination_tls_failure_and_unavailable_proxy(self):
        async def scenario():
            proxy = os.environ["SCOUT_HTTP_E2E_PROXY_URL"]
            transport = AiohttpSquidTransport(proxy)
            self.configure_dns({"private.fixture.test": {"A": [SINK_IP]}})
            with self.assertRaises(aiohttp.ClientHttpProxyError) as denied:
                await transport.get("https://private.fixture.test/", 1024, 2)
            self.assertEqual(denied.exception.status, 403)

            with self.assertRaises(aiohttp.ClientHttpProxyError) as unregistered:
                await transport.get("https://unregistered.invalid/", 1024, 2)
            self.assertEqual(unregistered.exception.status, 403)

            with (
                mock.patch.dict(os.environ, {"SSL_CERT_FILE": ""}),
                self.assertRaises(aiohttp.ClientError),
            ):
                await transport.get("https://catalog.fixture.test/docs/page", 32768, 2)

            unavailable = AiohttpSquidTransport(proxy)
            unavailable.proxy_url = "http://127.0.0.1:1"
            with self.assertRaises(aiohttp.ClientError):
                await unavailable.get(
                    "https://catalog.fixture.test/docs/page", 32768, 2
                )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
