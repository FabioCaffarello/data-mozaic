"""Controlled local tests for the distinct live Scrapy gateway handler."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

from collector_engineering.reference_runtime import RuntimeFailure
from collector_engineering.scrapy_live_runtime import (
    GatewayDownloadHandler,
    LiveRequestGuard,
    live_handler_settings,
)
from scrapy import Request


class _Gateway(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, bytes, dict[str, str]]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.__class__.received.append((self.path, body, dict(self.headers.items())))
        if json.loads(body).get("url", "").endswith("/slow"):
            time.sleep(0.5)
        payload = b'{"records":[{"id":1}]}'
        if json.loads(body).get("url", "").endswith("/denied"):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b'{"error":"redirect_denied"}')))
            self.end_headers()
            self.wfile.write(b'{"error":"redirect_denied"}')
            return
        reference_only = (
            self.headers.get("X-Mozaic-Response-Mode") == "evidence-reference"
        )
        response_body = b"" if reference_only else payload
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Mozaic-Fetch-ID", "f" * 32)
        self.send_header("X-Mozaic-Evidence-Ref", "f" * 32)
        self.send_header(
            "X-Mozaic-Body-SHA256", "sha256:" + hashlib.sha256(payload).hexdigest()
        )
        self.send_header("X-Mozaic-Body-Size", str(len(payload)))
        if reference_only:
            self.send_header("X-Mozaic-Response-Mode", "evidence-reference")
        self.send_header("X-Private-Internal", "must-not-reach-worker")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        try:
            self.wfile.write(response_body)
        except OSError:
            # The cancellation test closes the gateway transport before this
            # deliberately slow controlled response completes.
            pass

    def log_message(self, format: str, *args: object) -> None:
        return


class ScrapyLiveRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        _Gateway.received.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.capability = Path(self.temp.name) / "capability"
        self.capability.write_text("a" * 48, encoding="ascii")
        self.capability.chmod(0o600)
        self.env = patch.dict(
            os.environ,
            {
                "MOZAIC_ACQUISITION_GATEWAY_URL": f"http://127.0.0.1:{self.server.server_port}",
                "MOZAIC_EXECUTION_CAPABILITY_FILE": str(self.capability),
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def test_scrapy_handler_delegates_fetch_and_bounds_evidence_headers(self) -> None:
        crawler = SimpleNamespace(settings=SimpleNamespace())
        handler = GatewayDownloadHandler(crawler)
        request = Request("https://source.test/v1/records?id=2")

        async def fetch_and_close():
            try:
                return await handler.download_request(request)
            finally:
                await handler.close()

        response = asyncio.run(fetch_and_close())
        self.assertEqual(response.body, b'{"records":[{"id":1}]}')
        self.assertEqual(response.headers.get(b"X-Mozaic-Fetch-ID"), b"f" * 32)
        self.assertIsNone(response.headers.get(b"X-Private-Internal"))
        self.assertEqual(len(_Gateway.received), 1)
        path, body, headers = _Gateway.received[0]
        self.assertEqual(path, "/v1/fetch")
        self.assertEqual(body, b'{"url":"https://source.test/v1/records?id=2"}')
        self.assertEqual(headers.get("Authorization"), "Bearer " + "a" * 48)
        self.assertEqual(headers.get("Host"), f"127.0.0.1:{self.server.server_port}")

    def test_raw_only_handler_receives_metadata_without_artifact_body(self) -> None:
        handler = GatewayDownloadHandler(SimpleNamespace(settings=SimpleNamespace()))
        request = Request(
            "https://source.test/archive", meta={"dm_evidence_reference_only": True}
        )

        async def fetch_and_close():
            try:
                return await handler.download_request(request)
            finally:
                await handler.close()

        response = asyncio.run(fetch_and_close())
        self.assertEqual(response.body, b"")
        self.assertEqual(
            response.headers.get(b"X-Mozaic-Response-Mode"), b"evidence-reference"
        )
        self.assertEqual(len(_Gateway.received), 1)
        _, _, headers = _Gateway.received[0]
        self.assertEqual(headers.get("X-Mozaic-Response-Mode"), "evidence-reference")

    def test_gateway_errors_without_evidence_headers_keep_their_failure_code(
        self,
    ) -> None:
        handler = GatewayDownloadHandler(SimpleNamespace(settings=SimpleNamespace()))

        async def fetch_and_close():
            try:
                await handler.download_request(Request("https://source.test/denied"))
            finally:
                await handler.close()

        with self.assertRaisesRegex(RuntimeFailure, "redirect_denied"):
            asyncio.run(fetch_and_close())

    def test_request_guard_rejects_method_and_routing_headers(self) -> None:
        guard = LiveRequestGuard()
        with self.assertRaises(RuntimeFailure):
            guard.process_request(
                Request("https://source.test/", method="POST"), SimpleNamespace()
            )
        with self.assertRaises(RuntimeFailure):
            guard.process_request(
                Request("https://source.test/", headers={"Host": "internal"}),
                SimpleNamespace(),
            )

    def test_cancellation_reaches_gateway_transport(self) -> None:
        handler = GatewayDownloadHandler(SimpleNamespace(settings=SimpleNamespace()))

        async def cancel_fetch():
            try:
                task = asyncio.create_task(
                    handler.download_request(Request("https://source.test/slow"))
                )
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                await handler.close()

        asyncio.run(cancel_fetch())
        self.assertTrue(handler.session is None or handler.session.closed)

    def test_settings_are_distinct_and_disable_builtin_network_handlers(self) -> None:
        settings = live_handler_settings()
        self.assertEqual(settings["BOT_NAME"], "mozaic-scrapy-live")
        self.assertEqual(
            settings["DOWNLOAD_HANDLERS"]["https"],
            "mozaic_bot_runtime.scrapy_live_runtime.GatewayDownloadHandler",
        )
        self.assertFalse(settings["RETRY_ENABLED"])
        self.assertFalse(settings["REDIRECT_ENABLED"])


if __name__ == "__main__":
    unittest.main()
