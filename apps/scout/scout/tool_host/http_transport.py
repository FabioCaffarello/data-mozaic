"""Bounded, authenticated Streamable HTTP transport for the Scout MCP host."""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 1_000_000
MAX_CONCURRENT_REQUESTS = 8
MAX_SESSIONS = 32


@dataclass(frozen=True)
class HTTPBrokerSettings:
    bearer_token: str
    host: str = "127.0.0.1:8765"
    origin: str = "http://127.0.0.1:8765"
    path: str = "/mcp"

    def __post_init__(self) -> None:
        try:
            token_bytes = self.bearer_token.encode("ascii")
        except UnicodeEncodeError as error:
            raise ValueError("broker bearer token must be ASCII") from error
        if not 32 <= len(token_bytes) <= 512:
            raise ValueError("broker bearer token must contain at least 32 characters")
        if self.host != "127.0.0.1:8765" or self.path != "/mcp":
            raise ValueError("MVP broker endpoint must use the fixed loopback address")
        if self.origin != "http://127.0.0.1:8765":
            raise ValueError("MVP broker Origin must use the fixed loopback address")


class _RequestGuard:
    def __init__(self, app: ASGIApp, settings: HTTPBrokerSettings):
        self.app = app
        self.settings = settings
        self.semaphore = anyio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: dict[bytes, list[bytes]] = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.lower(), []).append(value)
        if len(headers.get(b"host", [])) != 1:
            await PlainTextResponse("invalid request headers", status_code=400)(
                scope, receive, send
            )
            return
        if len(headers.get(b"authorization", [])) > 1:
            await PlainTextResponse("invalid request headers", status_code=400)(
                scope, receive, send
            )
            return
        if len(headers.get(b"origin", [])) > 1:
            await PlainTextResponse("invalid origin", status_code=403)(
                scope, receive, send
            )
            return
        host = headers[b"host"][0].decode("latin-1")
        origin = headers.get(b"origin", [None])[0]
        authorization = headers.get(b"authorization", [b""])[0]
        if host != self.settings.host:
            await PlainTextResponse("invalid host", status_code=400)(
                scope, receive, send
            )
            return
        if origin is not None and origin.decode("latin-1") != self.settings.origin:
            await PlainTextResponse("invalid origin", status_code=403)(
                scope, receive, send
            )
            return
        expected = f"Bearer {self.settings.bearer_token}".encode("ascii")
        if not hmac.compare_digest(authorization, expected):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(
                scope, receive, send
            )
            return
        if scope.get("path") not in {self.settings.path, "/healthz"}:
            await PlainTextResponse("not found", status_code=404)(scope, receive, send)
            return
        if scope.get("query_string"):
            await PlainTextResponse(
                "query parameters are not supported", status_code=400
            )(scope, receive, send)
            return
        if scope["path"] == "/healthz":
            if scope["method"] != "GET":
                await PlainTextResponse("method not allowed", status_code=405)(
                    scope, receive, send
                )
            else:
                await JSONResponse({"status": "ok"})(scope, receive, send)
            return
        if scope["method"] not in {"GET", "POST", "DELETE"}:
            await PlainTextResponse("method not allowed", status_code=405)(
                scope, receive, send
            )
            return
        content_length_values = headers.get(b"content-length", [])
        if len(content_length_values) > 1:
            await PlainTextResponse("invalid content length", status_code=400)(
                scope, receive, send
            )
            return
        content_length = content_length_values[0] if content_length_values else None
        if content_length:
            try:
                if int(content_length) > MAX_REQUEST_BODY_BYTES:
                    await PlainTextResponse("request too large", status_code=413)(
                        scope, receive, send
                    )
                    return
            except ValueError:
                await PlainTextResponse("invalid content length", status_code=400)(
                    scope, receive, send
                )
                return
        async with self.semaphore:
            await self.app(scope, receive, send)


def create_streamable_http_app(server: Server, settings: HTTPBrokerSettings) -> ASGIApp:
    """Expose the existing MCP Server through bounded Streamable HTTP."""
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=False,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        max_sessions=MAX_SESSIONS,
        session_idle_timeout=900,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.host],
            allowed_origins=[settings.origin],
        ),
    )

    @asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    from starlette.applications import Starlette

    app = Starlette(
        routes=[
            Route(
                "/healthz",
                lambda _request: JSONResponse({"status": "ok"}),
                methods=["GET"],
            ),
            Mount("/", app=manager.handle_request),
        ],
        lifespan=lifespan,
    )
    return _RequestGuard(app, settings)


def settings_from_environment() -> HTTPBrokerSettings:
    token = os.environ.get("SCOUT_BROKER_TOKEN", "")
    if not token:
        raise RuntimeError("SCOUT_BROKER_TOKEN is required")
    return HTTPBrokerSettings(token)
