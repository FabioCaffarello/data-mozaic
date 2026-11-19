"""Real MCP host, PostgreSQL, Squid, TLS fixture, custody, and replay test."""

from __future__ import annotations

import asyncio
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
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from psycopg import connect
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier

ROOT = Path(__file__).resolve().parents[2]
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

    def test_authorized_https_mcp_custody_transcript_and_restart_replay(self):
        async def scenario():
            from scout.domain import SourceRef

            source = SourceRef("e2e-catalog", "revision-7")
            self.configure_dns(
                {
                    "catalog.fixture.test": {
                        "A": ["172.30.91.10"],
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
                    }
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
                    }
                ],
            }
            policy_path.write_text(json.dumps(policy), encoding="utf-8")

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
                args=["-m", "scout.tool_host"],
                env=env,
            )
            source_args = {
                "source_id": source.source_id,
                "revision": source.revision,
                "objective": "verify controlled HTTPS discovery and replay",
            }
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
                self.assertIn("scout_fetch", tools)
                self.assertFalse(
                    tools["scout_fetch"].inputSchema["additionalProperties"]
                )
                self.assertEqual(
                    set(tools["scout_fetch"].inputSchema["properties"]),
                    {"session_id", "expected_version", "url"},
                )

                started = await client.call_tool("scout_start", source_args)
                started_value = json.loads(started.content[0].text)
                self.assertTrue(started_value["ok"], started_value)
                session_id = started_value["session_id"]
                fetch_args = {
                    "session_id": session_id,
                    "expected_version": 0,
                    "url": "https://catalog.fixture.test/docs/page",
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
                import hashlib

                self.assertEqual(
                    hashlib.sha256(body).hexdigest(), artifact_ref["sha256"]
                )
                self.assertEqual(len(body), artifact_ref["size_bytes"])
                self.assertIn(b"fixture-secret-value", body)

                retry = await client.call_tool("scout_fetch", fetch_args)
                replay = json.loads(retry.content[0].text)
                self.assertEqual(replay, first)

                finding = await client.call_tool(
                    "scout_record_finding",
                    {
                        "session_id": session_id,
                        "expected_version": 1,
                        "statement": "The controlled catalog page links to its next page.",
                        "epistemic_status": "observation",
                        "confidence": 1.0,
                        "manifest_ids": [first["manifest_id"]],
                        "verification_note": "Observed in captured HTTPS fixture bytes.",
                    },
                )
                finding_value = json.loads(finding.content[0].text)
                self.assertTrue(finding_value["ok"], finding_value)
                proposal = await client.call_tool(
                    "scout_propose",
                    {
                        "session_id": session_id,
                        "expected_version": 2,
                        "summary": "Draft the controlled catalog capture.",
                        "finding_ids": [finding_value["finding_ids"][-1]],
                        "manifest_ids": [first["manifest_id"]],
                        "assumptions": ["The response is a local test fixture."],
                        "risks": ["Local adapters are not production integrations."],
                        "required_capabilities": ["http.fetch", "links.discover"],
                    },
                )
                self.assertTrue(json.loads(proposal.content[0].text)["ok"])
                exported = await client.call_tool(
                    "scout_export_transcript", {"session_id": session_id}
                )
                exported_value = json.loads(exported.content[0].text)
                self.assertTrue(exported_value["ok"], exported_value)
                transcript = json.loads(exported_value["transcript_json"])
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

            persisted = self.repository.get_session(session_id)
            self.assertEqual(persisted.principal, principal)
            self.assertEqual(persisted.source, source)
            self.assertEqual(len(persisted.observations), 1)
            self.assertEqual(persisted.remaining_requests, 3)
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
            self.assertEqual(budget, (1,))
            self.assertEqual(request_count, 1)

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
                "single.fixture.test": {"A": ["172.30.91.10"], "ttl": 60},
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
                    "A": ["172.30.91.10"],
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
                {"private.fixture.test": {"A": ["172.30.91.11"]}},
            ),
            (
                "mixed-a.fixture.test",
                {"mixed-a.fixture.test": {"A": ["172.30.91.10", "172.30.91.11"]}},
            ),
            (
                "mixed-aaaa.fixture.test",
                {
                    "mixed-aaaa.fixture.test": {
                        "A": ["172.30.91.10"],
                        "AAAA": ["::1"],
                    }
                },
            ),
            (
                "special.fixture.test",
                {
                    "special.fixture.test": {
                        "A": ["172.30.91.10"],
                        "AAAA": ["::ffff:172.30.91.11", "5f00::1"],
                    }
                },
            ),
            (
                "retry.fixture.test",
                {"retry.fixture.test": {"A": ["203.0.113.7", "172.30.91.11"]}},
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
                    "A": ["172.30.91.10"],
                    "ttl": 0,
                    "next": {"A": ["172.30.91.11"], "ttl": 60},
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
                args=["-m", "scout.tool_host"],
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
                            "next": {"A": ["172.30.91.11"], "ttl": 0},
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
                            answer.get("value") == "172.30.91.11"
                            for answer in item["answers"]
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
                            "A": ["172.30.91.10"],
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
            self.configure_dns({"private.fixture.test": {"A": ["172.30.91.11"]}})
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
