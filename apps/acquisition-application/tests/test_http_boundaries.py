from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest import TestCase

from acquisition_application.adapters.artifact_custody import ArtifactCustodyHTTPClient
from acquisition_application.live_evidence import _fetch_trusted_evidence
from acquisition_application.result_bundle import FixtureImportError


class RedirectServerHandler(BaseHTTPRequestHandler):
    follow_up_requests = 0

    def do_GET(self):
        if self.path != "/follow-up":
            self.send_response(302)
            self.send_header("Location", "/follow-up")
            self.end_headers()
            return
        type(self).follow_up_requests += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format, *args):
        pass


class AuthenticatedHTTPBoundaryTests(TestCase):
    def setUp(self):
        RedirectServerHandler.follow_up_requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectServerHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def test_custody_client_does_not_follow_authenticated_redirect(self):
        client = ArtifactCustodyHTTPClient(self.endpoint, "test-token")

        with self.assertRaises(FixtureImportError):
            client._request(
                "/redirect", method="GET", tenant_id="tenant-test", body=b""
            )

        self.assertEqual(RedirectServerHandler.follow_up_requests, 0)

    def test_gateway_evidence_client_does_not_follow_redirect(self):
        with self.assertRaises(FixtureImportError):
            _fetch_trusted_evidence(
                endpoint=self.endpoint,
                token="test-token",
                fetch_id="a" * 32,
                tenant_id="tenant-test",
                run_id="run-test",
                attempt_id="attempt-test",
                generation=1,
                max_body_bytes=1024,
            )

        self.assertEqual(RedirectServerHandler.follow_up_requests, 0)
