"""HTTP-only transport with DNS results validated and pinned for each connection."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from typing import Protocol

import aiohttp
from aiohttp.abc import AbstractResolver

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


class PublicAddressResolver(AbstractResolver):
    """Resolve and return the exact public IPs that the connector will use."""

    def __init__(self):
        self._resolved_hosts = set()

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        if host in self._resolved_hosts:
            raise TargetRejected(
                "a logical HTTP request may resolve its host only once"
            )
        self._resolved_hosts.add(host)
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            loop = asyncio.get_running_loop()
            records = await loop.getaddrinfo(
                host, port, family=family, type=socket.SOCK_STREAM
            )
            values = {(item[0], item[4][0]) for item in records}
            if not values:
                raise TargetRejected("DNS returned no address")
            if any(not ipaddress.ip_address(value).is_global for _, value in values):
                raise TargetRejected("DNS resolved to a non-public address")
        else:
            if not address.is_global:
                raise TargetRejected("non-public IP literals are prohibited")
            values = {
                (
                    socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                    address.compressed,
                )
            }
        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": address_family,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
            for address_family, ip in sorted(values)
        ]

    async def close(self):
        return None


class AiohttpPinnedTransport:
    """Mature HTTP/TLS client; redirects and budgets are owned by the caller."""

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
            resolver=PublicAddressResolver(),
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
            client.get(target.url, allow_redirects=False) as response,
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
