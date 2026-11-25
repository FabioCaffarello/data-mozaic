"""Narrow HTTP adapter for gateway requests to BC04 operation admission.

The endpoint is intended to be hosted by the BC04 coordinator process. It is
not an independent identity service and it never accepts a worker policy.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler
from typing import Any, Protocol

from execution_coordination.domain import CoordinationError

_MAX_BODY = 16_384


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


class AcquisitionOperationAuthorizationPort(Protocol):
    def authorize_acquisition_operation(
        self,
        capability: Mapping[str, object],
        *,
        operation_id: str,
        target_digest: str,
    ) -> Mapping[str, object]: ...


def acquisition_authorization_handler(
    coordinator: AcquisitionOperationAuthorizationPort, *, bearer_token: str
) -> type[BaseHTTPRequestHandler]:
    """Create a handler scoped to one coordinator and gateway service token."""
    if not bearer_token or len(bearer_token) < 32:
        raise ValueError(
            "gateway authorization token must contain at least 32 characters"
        )

    class Handler(BaseHTTPRequestHandler):
        server_version = "DataMozaic-BC04"
        sys_version = ""

        def do_POST(self) -> None:
            if self.path != "/internal/v1/acquisition-operations/admit":
                self._reply(404, {"error": "route_not_found"})
                return
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + bearer_token
            ):
                self._reply(401, {"error": "service_authentication_required"})
                return
            if (
                self.headers.get("Transfer-Encoding")
                or self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                self._reply(400, {"error": "request_invalid"})
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 1 <= length <= _MAX_BODY:
                    raise ValueError("request size invalid")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("request body incomplete")
                request = json.loads(body, object_pairs_hook=_unique_object)
                if not isinstance(request, dict) or set(request) != {
                    "capability",
                    "operation_id",
                    "target_digest",
                }:
                    raise ValueError("request fields invalid")
                capability = request["capability"]
                operation_id = request["operation_id"]
                target_digest = request["target_digest"]
                if (
                    not isinstance(capability, Mapping)
                    or not isinstance(operation_id, str)
                    or not isinstance(target_digest, str)
                ):
                    raise TypeError("request types invalid")
            except (TypeError, ValueError):
                self._reply(400, {"error": "request_invalid"})
                return
            try:
                admitted = coordinator.authorize_acquisition_operation(
                    capability,
                    operation_id=operation_id,
                    target_digest=target_digest,
                )
            except CoordinationError:
                self._reply(403, {"error": "current_attempt_denied"})
                return
            self._reply(
                200,
                {
                    "operation_id": admitted["operation_id"],
                    "admitted_at": admitted["admitted_at"].isoformat(),
                },
            )

        def do_GET(self) -> None:
            self._reply(404, {"error": "route_not_found"})

        def log_message(self, _format: str, *_args: object) -> None:
            # Authorization bodies and capability fields must never enter logs.
            return

        def _reply(self, status: int, payload: Mapping[str, object]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler
