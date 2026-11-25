from __future__ import annotations

import json
import threading
import unittest
from datetime import UTC, datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

from execution_coordination.adapters.acquisition_authorization_http import (
    acquisition_authorization_handler,
)
from execution_coordination.domain import CoordinationError


class StubCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], str, str]] = []
        self.deny = False

    def authorize_acquisition_operation(
        self, capability, *, operation_id, target_digest
    ):
        self.calls.append((dict(capability), operation_id, target_digest))
        if self.deny:
            raise CoordinationError("stale attempt")
        return {"operation_id": operation_id, "admitted_at": datetime.now(UTC)}


class AcquisitionAuthorizationHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = StubCoordinator()
        handler = acquisition_authorization_handler(
            self.coordinator, bearer_token="x" * 40
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(self, payload: bytes, *, token: str = "x" * 40):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        connection.request(
            "POST",
            "/internal/v1/acquisition-operations/admit",
            body=payload,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_authenticated_operation_is_forwarded_and_acknowledged(self):
        request = {
            "capability": {"capability_id": "cap-test"},
            "operation_id": "op-test",
            "target_digest": "sha256:" + "a" * 64,
        }
        status, body = self._post(json.dumps(request).encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["operation_id"], "op-test")
        self.assertEqual(
            self.coordinator.calls[0][1:], ("op-test", request["target_digest"])
        )

    def test_unauthenticated_and_stale_operations_are_denied(self):
        request = {
            "capability": {"capability_id": "cap-test"},
            "operation_id": "op-test",
            "target_digest": "sha256:" + "a" * 64,
        }
        self.assertEqual(self._post(json.dumps(request).encode(), token="bad")[0], 401)
        self.coordinator.deny = True
        self.assertEqual(self._post(json.dumps(request).encode())[0], 403)

    def test_duplicate_json_members_fail_closed(self):
        body = (
            b'{"capability":{},"capability":{},"operation_id":"op","target_digest":"sha256:'
            + b"a" * 64
            + b'"}'
        )
        status, _ = self._post(body)
        self.assertEqual(status, 400)
        self.assertEqual(self.coordinator.calls, [])


if __name__ == "__main__":
    unittest.main()
