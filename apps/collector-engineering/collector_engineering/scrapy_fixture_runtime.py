"""Scrapy-only fixture downloader and bounded item pipeline for BC15."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from scrapy import Request, Spider
from scrapy.core.downloader.handlers.base import BaseDownloadHandler
from scrapy.http import Response
from scrapy.responsetypes import responsetypes

from .reference_runtime import RuntimeFailure, fetch_fixture

SCRAPY_RUNTIME_VERSION = "scrapy-fixture-runtime/1.1.0"
REQUEST_META_KEYS = {
    "dm_operation",
    "dm_page_index",
    "dm_discovered_from",
    "download_slot",
    "_download_slot_stamp",
}


def handler_settings() -> dict[str, Any]:
    """Return a closed, versioned Scrapy settings map for this backend."""
    from scrapy.settings.default_settings import (
        DOWNLOAD_HANDLERS_BASE,
        DOWNLOADER_MIDDLEWARES_BASE,
        EXTENSIONS_BASE,
        SPIDER_MIDDLEWARES_BASE,
    )

    return {
        "BOT_NAME": "mozaic-scrapy-fixture",
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "CONCURRENT_ITEMS": 1,
        "DOWNLOAD_TIMEOUT": 5,
        "DOWNLOAD_MAXSIZE": 2_000_000,
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
            "http": "mozaic_bot_runtime.scrapy_fixture_runtime.FixtureOnlyDownloadHandler",
            "https": "mozaic_bot_runtime.scrapy_fixture_runtime.FixtureOnlyDownloadHandler",
        },
        "DOWNLOAD_HANDLERS_BY_NAME": {},
        "DOWNLOADER_MIDDLEWARES": {
            **{name: None for name in DOWNLOADER_MIDDLEWARES_BASE},
            "mozaic_bot_runtime.scrapy_fixture_runtime.RequestGuardMiddleware": 1,
        },
        "SPIDER_MIDDLEWARES": {name: None for name in SPIDER_MIDDLEWARES_BASE},
        "EXTENSIONS": {name: None for name in EXTENSIONS_BASE},
        "ITEM_PIPELINES": {
            "mozaic_bot_runtime.scrapy_fixture_runtime.BoundedRecordPipeline": 100
        },
        "JOBDIR": None,
        "DUPEFILTER_CLASS": "scrapy.dupefilters.RFPDupeFilter",
        "SCHEDULER_MEMORY_QUEUE": "scrapy.squeues.FifoMemoryQueue",
        "SCHEDULER_DISK_QUEUE": "scrapy.squeues.PickleFifoDiskQueue",
        "SCHEDULER_DEBUG": True,
        "DEPTH_LIMIT": 0,
        "DOWNLOAD_DELAY": 0,
        "AUTOTHROTTLE_ENABLED": False,
        "HTTPCACHE_ENABLED": False,
        "DNSCACHE_ENABLED": False,
        "REQUEST_FINGERPRINTER_CLASS": "scrapy.utils.request.RequestFingerprinter",
    }


class RequestGuardMiddleware:
    """Reject downloader metadata and schemes outside the generated request API."""

    def process_request(self, request: Request, spider: Spider) -> None:
        if set(request.meta) - REQUEST_META_KEYS:
            raise RuntimeFailure(
                "REQUEST_META_UNSUPPORTED",
                f"request metadata is not allowed: {sorted(set(request.meta) - REQUEST_META_KEYS)!r}",
            )
        if request.method != "GET" or request.body:
            raise RuntimeFailure(
                "REQUEST_UNSUPPORTED", "only empty GET requests are supported"
            )
        if request.url.split(":", 1)[0].lower() not in {"http", "https"}:
            raise RuntimeFailure("SCHEME_UNSUPPORTED", "URL scheme is not supported")


class FixtureOnlyDownloadHandler(BaseDownloadHandler):
    """Map exact, pre-verified fixture identities to Scrapy Responses."""

    lazy = False

    async def download_request(self, request: Request) -> Response:
        spider = self.crawler.spider
        if not isinstance(spider, GeneratedSpiderProtocol):
            raise RuntimeFailure(
                "SPIDER_UNSUPPORTED", "fixture handler requires generated spider"
            )
        if request.meta.get("download_handler") is not None:
            raise RuntimeFailure(
                "DOWNLOAD_HANDLER_OVERRIDE", "download handler override is forbidden"
            )
        if set(request.meta) - REQUEST_META_KEYS:
            raise RuntimeFailure(
                "REQUEST_META_UNSUPPORTED",
                f"request metadata is not allowed: {sorted(set(request.meta) - REQUEST_META_KEYS)!r}",
            )
        url = request.url
        if not spider.scope_check(url):
            raise RuntimeFailure(
                "URL_OUT_OF_SCOPE", f"URL is outside fixture scope: {url}"
            )
        expected = spider.fixture_by_url.get(url)
        if expected is None:
            raise RuntimeFailure(
                "FIXTURE_NOT_IN_BUILD", f"URL is not in the verified BotBuild: {url}"
            )
        if request.meta.get("dm_discovered_from") is not None:
            observed_from = request.meta["dm_discovered_from"]
            if expected.get("discovered_from") != observed_from:
                raise RuntimeFailure(
                    "LINK_NOT_OBSERVED",
                    f"fixture does not prove link from {observed_from}",
                )
        document = fetch_fixture(
            spider.runtime_context,
            url=url,
            content_type=expected["content_type"],
            evidence_refs=[expected["evidence_ref"]],
        )
        headers = {b"Content-Type": expected["content_type"].encode("ascii")}
        response_cls = responsetypes.from_args(
            headers=headers, url=url, body=document["body"]
        )
        return response_cls(
            url=url,
            status=expected["status"],
            headers=headers,
            body=document["body"],
            request=request,
        )


class BoundedRecordPipeline:
    """Buffer bounded items; final files are published only after crawl success."""

    def process_item(self, item: dict, spider: Spider) -> dict:
        if not isinstance(spider, GeneratedSpiderProtocol):
            raise RuntimeFailure(
                "SPIDER_UNSUPPORTED", "record sink requires generated spider"
            )
        key = item.get("_sort_key")
        record = item.get("record")
        if (
            not isinstance(key, (tuple, list))
            or len(key) != 2
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in key
            )
            or not isinstance(record, dict)
        ):
            raise RuntimeFailure("RECORD_INVALID", "pipeline item shape is invalid")
        if len(spider.records) >= spider.budgets["max_records"]:
            raise RuntimeFailure("BUDGET_RECORDS_EXCEEDED", "record budget exhausted")
        encoded_size = len(canonical_record(record)) + 1
        if (
            spider.buffered_output_bytes + encoded_size
            > spider.budgets["max_output_bytes"]
        ):
            raise RuntimeFailure(
                "BUDGET_OUTPUT_BYTES_EXCEEDED", "output budget exhausted"
            )
        spider.buffered_output_bytes += encoded_size
        spider.records.append((tuple(key), record))
        return item


def canonical_record(record: dict) -> bytes:
    return json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def publish_output(spider: Any, output_dir: Path, *, engine: dict[str, str]) -> dict:
    """Publish complete output after crawl success, rolling back failed commits."""
    if spider.failure is not None:
        raise RuntimeFailure("SCRAPY_CRAWL_FAILED", str(spider.failure))
    if output_dir.exists() or output_dir.is_symlink():
        raise RuntimeFailure("OUTPUT_PATH_UNSAFE", "output destination already exists")
    staging = output_dir.with_name(f".{output_dir.name}.partial")
    if staging.exists() or staging.is_symlink():
        raise RuntimeFailure(
            "OUTPUT_PATH_UNSAFE", "partial output destination already exists"
        )
    staging.mkdir(mode=0o700, parents=False)
    context = spider.runtime_context
    context.output_root = staging
    records = [record for _, record in sorted(spider.records, key=lambda pair: pair[0])]
    try:
        report = spider.runtime_module.write_records(context, records)
        report["engine"] = engine
        stats = spider.crawler.stats.get_stats() if spider.crawler.stats else {}
        report["engine_stats"] = {
            key: stats[key]
            for key in (
                "downloader/request_count",
                "downloader/response_count",
                "item_scraped_count",
                "scheduler/enqueued",
                "scheduler/dequeued",
                "dupefilter/filtered",
            )
            if key in stats
        }
        if spider.raw_output:
            spider.runtime_module.write_raw_references(context, spider.documents)
        spider.runtime_module.write_report(context, report)
        os.replace(staging, output_dir)
        return report
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(spider.context_root, ignore_errors=True)


class GeneratedSpiderProtocol:
    """Structural type marker used only for defensive runtime checks."""
