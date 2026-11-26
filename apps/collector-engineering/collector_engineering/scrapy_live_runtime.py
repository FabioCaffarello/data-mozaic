"""Scrapy HTTP transport delegated to the trusted acquisition gateway.

This module is deliberately separate from the fixture runtime. The worker only
connects to the gateway; this handler never opens a connection to a source URL.
The container network remains the enforcement boundary against other sockets.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import aiohttp
from scrapy import Request, Spider
from scrapy.core.downloader.handlers.base import BaseDownloadHandler
from scrapy.http import Response
from scrapy.responsetypes import responsetypes

from .reference_runtime import RuntimeFailure

LIVE_RUNTIME_VERSION = "scrapy-live-http-runtime/1.2.0"
MAX_GATEWAY_BODY = 8192
MAX_RESPONSE_BYTES = 2_000_000
ALLOWED_RESPONSE_HEADERS = {
    "x-mozaic-fetch-id",
    "x-mozaic-evidence-ref",
    "x-mozaic-source-status",
    "x-mozaic-body-sha256",
    "x-mozaic-acquired-at",
    "x-mozaic-body-representation",
    "x-mozaic-media-type",
}


def live_handler_settings() -> dict[str, Any]:
    """Closed Scrapy settings for a separately admitted live worker."""
    from scrapy.settings.default_settings import (
        DOWNLOAD_HANDLERS_BASE,
        DOWNLOADER_MIDDLEWARES_BASE,
        EXTENSIONS_BASE,
        SPIDER_MIDDLEWARES_BASE,
    )

    return {
        "BOT_NAME": "mozaic-scrapy-live",
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "CONCURRENT_ITEMS": 1,
        "DOWNLOAD_TIMEOUT": 305,
        "DOWNLOAD_MAXSIZE": MAX_RESPONSE_BYTES,
        "DOWNLOAD_FAIL_ON_DATALOSS": True,
        "RETRY_ENABLED": False,
        "REDIRECT_ENABLED": False,
        "METAREFRESH_ENABLED": False,
        "ROBOTSTXT_OBEY": False,
        "COOKIES_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "LOG_ENABLED": False,
        "FEEDS": {},
        "FEED_EXPORTERS": {},
        "DOWNLOAD_HANDLERS": {
            **{scheme: None for scheme in DOWNLOAD_HANDLERS_BASE},
            "http": "mozaic_bot_runtime.scrapy_live_runtime.GatewayDownloadHandler",
            "https": "mozaic_bot_runtime.scrapy_live_runtime.GatewayDownloadHandler",
        },
        "DOWNLOAD_HANDLERS_BY_NAME": {},
        "DOWNLOADER_MIDDLEWARES": {
            **{name: None for name in DOWNLOADER_MIDDLEWARES_BASE},
            "mozaic_bot_runtime.scrapy_live_runtime.LiveRequestGuard": 1,
        },
        "SPIDER_MIDDLEWARES": {name: None for name in SPIDER_MIDDLEWARES_BASE},
        "EXTENSIONS": {name: None for name in EXTENSIONS_BASE},
        "ITEM_PIPELINES": {},
        "JOBDIR": None,
        "DUPEFILTER_CLASS": "scrapy.dupefilters.RFPDupeFilter",
        "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
        "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
        "DEPTH_LIMIT": 0,
        "DOWNLOAD_DELAY": 0,
        "AUTOTHROTTLE_ENABLED": False,
        "HTTPCACHE_ENABLED": False,
        "DNSCACHE_ENABLED": False,
    }


class LiveRequestGuard:
    """Reject handler overrides, non-GET requests and worker credentials."""

    def process_request(self, request: Request, spider: Spider) -> None:
        if request.method != "GET" or request.body:
            raise RuntimeFailure("REQUEST_UNSUPPORTED", "live runtime is GET-only")
        if request.meta.get("download_handler") is not None:
            raise RuntimeFailure(
                "DOWNLOAD_HANDLER_OVERRIDE", "handler override is forbidden"
            )
        if any(
            name.lower()
            in {b"host", b"authorization", b"proxy-authorization", b"cookie"}
            for name in request.headers
        ):
            raise RuntimeFailure(
                "REQUEST_HEADER_DENIED",
                "worker authentication and routing headers are forbidden",
            )


class GatewayDownloadHandler(BaseDownloadHandler):
    """Delegate each source fetch to the gateway using the attempt capability."""

    lazy = False

    def __init__(self, crawler: Any) -> None:
        super().__init__(crawler)
        gateway = os.environ.get("MOZAIC_ACQUISITION_GATEWAY_URL", "")
        if not gateway.startswith(("http://", "https://")) or len(gateway) > 512:
            raise RuntimeFailure(
                "GATEWAY_CONFIGURATION_INVALID", "gateway endpoint is missing"
            )
        self.endpoint = gateway.rstrip("/") + "/v1/fetch"
        capability_path = Path(os.environ.get("MOZAIC_EXECUTION_CAPABILITY_FILE", ""))
        try:
            info = capability_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & (
                stat.S_IWGRP | stat.S_IWOTH
            ):
                raise OSError("unsafe capability file")
            raw = capability_path.read_bytes()
            if len(raw) > 4096:
                raise OSError("capability file too large")
            capability = raw.decode("ascii").strip()
            if len(capability) < 32 or any(
                ord(ch) < 33 or ord(ch) > 126 for ch in capability
            ):
                raise OSError("capability encoding invalid")
        except (OSError, UnicodeError) as error:
            raise RuntimeFailure(
                "CAPABILITY_UNAVAILABLE", "current attempt capability is unavailable"
            ) from error
        self.authorization = "Bearer " + capability
        self.session: aiohttp.ClientSession | None = None

    @classmethod
    def from_crawler(cls, crawler: Any) -> GatewayDownloadHandler:
        return cls(crawler)

    async def download_request(self, request: Request) -> Response:
        if (
            request.method != "GET"
            or request.body
            or request.meta.get("download_handler") is not None
        ):
            raise RuntimeFailure("REQUEST_UNSUPPORTED", "live runtime accepts only GET")
        if any(
            name.lower()
            in {b"host", b"authorization", b"proxy-authorization", b"cookie"}
            for name in request.headers
        ):
            raise RuntimeFailure(
                "REQUEST_HEADER_DENIED", "worker supplied a protected header"
            )
        if self.session is None:
            timeout = aiohttp.ClientTimeout(total=305, connect=5, sock_read=305)
            self.session = aiohttp.ClientSession(
                trust_env=False,
                timeout=timeout,
                auto_decompress=False,
                connector=aiohttp.TCPConnector(limit=1, limit_per_host=1),
                max_line_size=8192,
                max_field_size=8192,
            )
        payload = json.dumps({"url": request.url}, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(payload) > MAX_GATEWAY_BODY:
            raise RuntimeFailure(
                "GATEWAY_REQUEST_TOO_LARGE", "gateway request exceeds bound"
            )
        try:
            async with self.session.post(
                self.endpoint,
                data=payload,
                headers={
                    "Authorization": self.authorization,
                    "Content-Type": "application/json",
                },
                allow_redirects=False,
            ) as gateway_response:
                status = gateway_response.status
                headers = gateway_response.headers
                body = await gateway_response.content.read(MAX_RESPONSE_BYTES + 1)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, TimeoutError) as error:
            raise RuntimeFailure(
                "GATEWAY_UNAVAILABLE", "acquisition gateway request failed"
            ) from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise RuntimeFailure(
                "GATEWAY_RESPONSE_TOO_LARGE", "gateway response exceeds runtime bound"
            )
        bounded = {
            name.lower().encode("ascii"): str(value).encode("latin-1", errors="replace")
            for name, value in headers.items()
            if name.lower() in ALLOWED_RESPONSE_HEADERS
            or name.lower() == "content-type"
        }
        response_class = responsetypes.from_args(
            headers=bounded, url=request.url, body=body
        )
        return response_class(
            url=request.url, status=status, headers=bounded, body=body, request=request
        )

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None
