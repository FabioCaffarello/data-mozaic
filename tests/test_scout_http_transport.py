"""Streamable HTTP transport security using the official MCP client SDK."""

from __future__ import annotations

import asyncio
import unittest

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.types import TextContent, Tool
from scout.tool_host.http_transport import (
    HTTPBrokerSettings,
    create_streamable_http_app,
)


class ScoutHTTPTransportTests(unittest.TestCase):
    def test_authenticated_streamable_http_and_rejected_requests(self):
        async def exercise():
            server = Server("scout-http-transport-test")

            @server.list_tools()
            async def list_tools():
                return [
                    Tool(
                        name="health",
                        description="synthetic test operation",
                        inputSchema={
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    )
                ]

            @server.call_tool()
            async def call_tool(name, arguments):
                if name != "health" or arguments:
                    raise ValueError("unsupported operation")
                return [TextContent(type="text", text="ok")]

            token = "test-token-" + "a" * 48
            app = create_streamable_http_app(server, HTTPBrokerSettings(token))
            config = uvicorn.Config(
                app, host="127.0.0.1", port=8765, lifespan="on", log_level="critical"
            )
            uvicorn_server = uvicorn.Server(config)
            task = asyncio.create_task(uvicorn_server.serve())
            try:
                for _ in range(100):
                    if uvicorn_server.started:
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(uvicorn_server.started)
                async with httpx.AsyncClient() as http:
                    unauthorized = await http.post(
                        "http://127.0.0.1:8765/mcp",
                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    )
                    self.assertEqual(401, unauthorized.status_code)
                    wrong_host = await http.post(
                        "http://127.0.0.1:8765/mcp",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Host": "attacker.test",
                        },
                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    )
                    self.assertEqual(400, wrong_host.status_code)
                    wrong_origin = await http.post(
                        "http://127.0.0.1:8765/mcp",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Origin": "http://attacker.test",
                        },
                        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    )
                    self.assertEqual(403, wrong_origin.status_code)
                    too_large = await http.post(
                        "http://127.0.0.1:8765/mcp",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Length": "1000001",
                        },
                        content=b"x" * 1_000_001,
                    )
                    self.assertEqual(413, too_large.status_code)
                http_client = httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}
                )
                async with (
                    http_client,
                    streamable_http_client(
                        "http://127.0.0.1:8765/mcp", http_client=http_client
                    ) as (read, write, _),
                    ClientSession(read, write) as client,
                ):
                    initialized = await client.initialize()
                    self.assertEqual(
                        "scout-http-transport-test", initialized.serverInfo.name
                    )
                    listed = await client.list_tools()
                    self.assertEqual(["health"], [tool.name for tool in listed.tools])
                    result = await client.call_tool("health", {})
                    self.assertEqual("ok", result.content[0].text)
            finally:
                uvicorn_server.should_exit = True
                await task

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
