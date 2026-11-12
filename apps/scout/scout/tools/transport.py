"""HTTP-only transport that requires the isolated Squid egress proxy."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp

from .url_policy import TargetRejected, normalize_url


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class HTTPTransportPort(Protocol):
    async def get(
        self, url: str, max_bytes: int, timeout_seconds: float
    ) -> HTTPResponse: ...


class AiohttpSquidTransport:
    """HTTP/TLS client that can only reach destinations through the local proxy."""

    def __init__(self, proxy_url: str):
        parts = urlsplit(proxy_url)
        if (
            parts.scheme != "http"
            or parts.hostname != "squid"
            or parts.port != 3128
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Scout transport requires the isolated squid:3128 proxy")
        self.proxy_url = proxy_url

    async def get(
        self, url: str, max_bytes: int, timeout_seconds: float
    ) -> HTTPResponse:
        target = normalize_url(url)
        timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
            connect=timeout_seconds,
            sock_read=timeout_seconds,
        )
        connector = aiohttp.TCPConnector(
            use_dns_cache=False,
            force_close=True,
            ssl=ssl.create_default_context(),
            limit=1,
        )
        async with (
            aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                trust_env=False,
                auto_decompress=False,
                headers={
                    "Accept-Encoding": "identity",
                    "User-Agent": "DataMozaic-Scout/0.1",
                },
            ) as client,
            client.get(
                target.url,
                proxy=self.proxy_url,
                allow_redirects=False,
            ) as response,
        ):
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError as error:
                    raise TargetRejected("invalid content length") from error
                if declared_size < 0 or declared_size > max_bytes:
                    raise TargetRejected("response exceeds approved byte limit")
            if response.headers.get("Content-Encoding", "identity").lower() not in (
                "",
                "identity",
            ):
                raise TargetRejected("compressed responses are not accepted")
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                if len(body) + len(chunk) > max_bytes:
                    raise TargetRejected("response exceeds approved byte limit")
                body.extend(chunk)
            headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower()
                in {
                    "content-type",
                    "content-length",
                    "etag",
                    "last-modified",
                    "location",
                }
            }
            return HTTPResponse(response.status, headers, bytes(body))
