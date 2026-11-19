"""Real MCP host, PostgreSQL, Squid, TLS fixture, custody, and replay test."""

from __future__ import annotations

import asyncio
import json
import os
import sys
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
        self.assertIn({"name": "single.fixture.test", "type": "A"}, questions)

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
        self.assertIn({"name": "cname.fixture.test", "type": "A"}, cname_questions)

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
        self.assertEqual(
            self.dns_state()["queries"].count(
                {"name": "rebinding.fixture.test", "type": "A"}
            ),
            1,
        )

        self.configure_dns(
            {"unavailable.fixture.test": {"rcode": 2, "A": [], "AAAA": []}}
        )
        with self.assertRaises(aiohttp.ClientError):
            asyncio.run(fetch("unavailable.fixture.test"))

        self.configure_dns({"dns-drop.fixture.test": {"drop": True}})
        with self.assertRaises((aiohttp.ClientError, TimeoutError)):
            asyncio.run(fetch("dns-drop.fixture.test"))
        self.assertIn(
            {"name": "dns-drop.fixture.test", "type": "A"},
            self.dns_state()["queries"],
        )

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
