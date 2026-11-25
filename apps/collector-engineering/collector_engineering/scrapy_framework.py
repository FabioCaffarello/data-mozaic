"""Versioned Scrapy lifecycle for immutable BC15 BotBuild plans.

The compiler supplies only a closed, deterministic plan. This module owns the
Scrapy process, spider lifecycle, operator dispatch, result assembly and cleanup.
Transport remains profile-specific in the fixture and gateway adapters.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from scrapy import Request, Spider, signals
from scrapy.crawler import CrawlerProcess
from scrapy.settings import Settings

from . import reference_runtime, scrapy_fixture_runtime
from .reference_runtime import RuntimeFailure

_FIXTURE_HANDLER = (
    "mozaic_bot_runtime.scrapy_fixture_runtime.FixtureOnlyDownloadHandler"
)
_LIVE_HANDLER = "mozaic_bot_runtime.scrapy_live_runtime.GatewayDownloadHandler"


def _runtime_root() -> Path:
    return Path(__file__).resolve().parent


def _verify_runtime(identity: dict[str, Any], *, live: bool = False) -> None:
    package_root = _runtime_root()
    framework_module = sys.modules.get(__name__)
    modules = [framework_module, reference_runtime, scrapy_fixture_runtime]
    if live:
        from . import scrapy_live_runtime

        modules.append(scrapy_live_runtime)
    if any(
        Path(module.__file__).resolve().parent != package_root for module in modules
    ):
        raise RuntimeFailure(
            "BUILD_RUNTIME_IMPORT_MISMATCH",
            "framework modules were not imported from this BotBuild",
        )
    from .identity import verify

    verify(package_root, identity["files"], identity["sha256"])


def _output_path(value: str) -> Path:
    output = Path(value).absolute()
    if (
        ".." in Path(value).parts
        or output.exists()
        or output.is_symlink()
        or output.parent.is_symlink()
        or not output.parent.is_dir()
    ):
        raise RuntimeFailure("OUTPUT_PATH_UNSAFE", "output path is unsafe")
    return output


def _verify_scrapy_tables(spider: Spider, *, profile: str) -> None:
    downloader = spider.crawler.engine.downloader
    handlers = downloader.handlers
    handler = _FIXTURE_HANDLER if profile == "fixture" else _LIVE_HANDLER
    if (
        set(handlers._paths) != {"http", "https"}
        or set(handlers._handlers) != {"http", "https"}
        or handlers._notconfigured
        or any(handlers._paths[scheme] != handler for scheme in ("http", "https"))
    ):
        raise RuntimeFailure("HANDLER_TABLE_UNSAFE", "effective handlers differ")
    middleware = downloader.middleware.middlewares
    wanted = "RequestGuardMiddleware" if profile == "fixture" else "LiveRequestGuard"
    if len(middleware) != 1 or type(middleware[0]).__name__ != wanted:
        raise RuntimeFailure("MIDDLEWARE_TABLE_UNSAFE", "downloader middleware differs")
    if profile == "fixture":
        if spider.crawler.engine.scraper.spidermw.middlewares:
            raise RuntimeFailure(
                "MIDDLEWARE_TABLE_UNSAFE", "spider middleware is enabled"
            )
        if spider.crawler.extensions.middlewares:
            raise RuntimeFailure(
                "EXTENSION_TABLE_UNSAFE", "Scrapy extensions are enabled"
            )
        pipelines = spider.crawler.engine.scraper.itemproc.middlewares
        if (
            len(pipelines) != 1
            or type(pipelines[0]).__name__ != "BoundedRecordPipeline"
        ):
            raise RuntimeFailure("PIPELINE_TABLE_UNSAFE", "record pipeline differs")


class FixturePlanSpider(Spider, scrapy_fixture_runtime.GeneratedSpiderProtocol):
    """Interpret the supported fixture plan through the no-network adapter."""

    name = "mozaic-generated-fixture-spider"

    def __init__(
        self, plan: dict[str, Any], fixture_root: str, output_root: str, **kwargs
    ):
        super().__init__(**kwargs)
        self.plan = plan
        self.fixture_root = Path(fixture_root).resolve(strict=True)
        self.output_root = Path(output_root).absolute()
        self.source = plan["source"]
        self.budgets = plan["budgets"]
        self.fetch = plan["fetch"]
        self.select = plan["select"]
        self.extract = plan["extract"]
        self.discover = plan.get("discover")
        self.page_limit = plan["page_limit"]
        self.raw_output = plan["raw_output"]
        self.fixture_by_url = {page["url"]: page for page in plan["fixtures"]}
        context_root = Path(tempfile.mkdtemp(prefix="mozaic-scrapy-context-")).resolve()
        self.runtime_module = reference_runtime
        try:
            runtime_context = reference_runtime.RuntimeContext.create(
                str(self.fixture_root),
                str(context_root),
                self.source,
                self.budgets,
                plan["fixtures"],
            )
        except BaseException:
            shutil.rmtree(context_root, ignore_errors=True)
            raise
        self.context_root = context_root
        self.runtime_context = runtime_context
        self.documents: list[dict[str, Any]] = []
        self.records: list[tuple[tuple[int, int], dict[str, Any]]] = []
        self.buffered_output_bytes = 0
        self.failure: str | None = None
        self.selected_count = 0
        self.discovered_urls: list[str] = []
        self.visited_urls = {self.fetch["inputs"]["url"]}

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._spider_error, signal=signals.spider_error)
        crawler.signals.connect(spider._item_error, signal=signals.item_error)
        crawler.signals.connect(spider._spider_closed, signal=signals.spider_closed)
        return spider

    def scope_check(self, url: str) -> bool:
        return reference_runtime._scoped(url, self.source)

    async def start(self):
        _verify_scrapy_tables(self, profile="fixture")
        yield Request(
            self.fetch["inputs"]["url"],
            callback=self.parse_page,
            errback=self._failed,
            meta={"dm_operation": self.fetch["operation_id"], "dm_page_index": 0},
        )

    def parse_page(self, response):
        _verify_scrapy_tables(self, profile="fixture")
        page_index = response.meta["dm_page_index"]
        document = self._document(response, page_index)
        self.documents.append(document)
        yield from self._process_document(document)
        if self.discover is None or page_index != 0:
            return
        inputs = self.discover["inputs"]
        link_set = reference_runtime.discover_links(
            self.runtime_context,
            document,
            selector=inputs["selector"],
            max_links=inputs["max_links"],
            evidence_refs=inputs["evidence_refs"],
        )
        for url in link_set["urls"]:
            if url in self.visited_urls:
                continue
            if len(self.documents) + len(self.discovered_urls) >= self.page_limit:
                break
            self.visited_urls.add(url)
            self.discovered_urls.append(url)
        for index, url in enumerate(self.discovered_urls, start=1):
            yield Request(
                url,
                callback=self.parse_page,
                errback=self._failed,
                meta={
                    "dm_operation": self.select["operation_id"],
                    "dm_page_index": index,
                    "dm_discovered_from": document["evidence_ref"],
                },
            )

    def _document(self, response, page_index: int) -> dict[str, Any]:
        expected = self.fixture_by_url.get(response.url)
        if expected is None:
            raise RuntimeFailure(
                "FIXTURE_NOT_IN_BUILD", "response URL is not in BotBuild"
            )
        digest = hashlib.sha256(response.body).hexdigest()
        if digest != expected["sha256"] or len(response.body) != expected["size_bytes"]:
            raise RuntimeFailure("FIXTURE_IDENTITY_MISMATCH", "fixture bytes differ")
        return {
            "url": response.url,
            "requested_url": response.request.url,
            "body": response.body,
            "sha256": digest,
            "evidence_ref": expected["evidence_ref"],
            "content_type": expected["content_type"],
            "status": response.status,
            "page_index": page_index,
        }

    def _process_document(self, document: dict[str, Any]):
        inputs = self.select["inputs"]
        if self.select["operator_type"] == "select.html_records":
            selected = reference_runtime.select_html_records(
                self.runtime_context,
                document,
                selector=inputs["selector"],
                cardinality=inputs.get("cardinality", "many"),
                max_matches=inputs["max_matches"],
                evidence_refs=inputs["evidence_refs"],
            )
        else:
            selected = reference_runtime.select_json_records(
                self.runtime_context,
                document,
                array_path=inputs["array_path"],
                max_matches=inputs["max_matches"],
                evidence_refs=inputs["evidence_refs"],
            )
        self.selected_count += len(selected)
        if (
            self.selected_count > inputs.get("max_matches", self.budgets["max_records"])
            or self.selected_count > self.budgets["max_records"]
        ):
            raise RuntimeFailure(
                "BUDGET_RECORDS_EXCEEDED", "selected records exceed budget"
            )
        extracted = reference_runtime.extract_fields(
            self.runtime_context,
            selected,
            fields=self.extract["inputs"]["fields"],
            operation_id=self.extract["operation_id"],
        )
        for item, record in zip(selected, extracted, strict=True):
            yield {
                "_sort_key": [document["page_index"], item["record_index"]],
                "record": record,
            }

    def _failed(self, failure):
        self.failure = failure.getErrorMessage()
        if self.crawler.engine is not None:
            self.crawler.engine.close_spider(self, reason="fatal")

    def _spider_error(self, failure, response, spider):
        self.failure = failure.getErrorMessage()

    def _item_error(self, item, response, spider, failure):
        self.failure = failure.getErrorMessage()

    def _spider_closed(self, spider, reason: str):
        if reason == "fatal" and self.failure is None:
            self.failure = "fatal request or callback failure"
        if reason in {"shutdown", "cancelled"} and self.failure is None:
            self.failure = f"EXECUTION_CANCELLED: Scrapy closed with reason {reason}"
        cardinality = self.select["inputs"].get("cardinality", "many")
        if cardinality == "one" and self.selected_count != 1:
            self.failure = (
                f"CARDINALITY_MISMATCH: expected one record, got {self.selected_count}"
            )
        elif cardinality == "optional_one" and self.selected_count > 1:
            self.failure = f"CARDINALITY_MISMATCH: expected at most one record, got {self.selected_count}"


class LivePlanSpider(Spider):
    """Interpret the live plan through the authorized gateway adapter only."""

    name = "mozaic-generated-live-spider"

    def __init__(self, plan: dict[str, Any], output_root: str, **kwargs):
        super().__init__(**kwargs)
        self.plan = plan
        self.output_root = Path(output_root).absolute()
        self.source = plan["source"]
        self.budgets = plan["budgets"]
        self.fetch = plan["fetch"]
        self.select = plan["select"]
        self.extract = plan["extract"]
        self.discover = plan.get("discover")
        self.pagination = plan.get("pagination")
        self.max_pages = (
            self.budgets["max_pages"]
            if self.pagination is None
            else self.pagination["inputs"]["max_pages"]
        )
        self.completion_mode = (
            "natural_end"
            if self.pagination is None
            else self.pagination["inputs"].get("completion_mode", "natural_end")
        )
        fixture_root = Path(tempfile.mkdtemp(prefix="dm-live-empty-fixtures-"))
        context_root = None
        try:
            context_root = Path(tempfile.mkdtemp(prefix="dm-live-context-"))
            runtime_context = reference_runtime.RuntimeContext.create(
                str(fixture_root),
                str(self.output_root),
                self.source,
                self.budgets,
                [],
            )
        except BaseException:
            shutil.rmtree(fixture_root, ignore_errors=True)
            if context_root is not None:
                shutil.rmtree(context_root, ignore_errors=True)
            raise
        self.fixture_root = fixture_root
        self.context_root = context_root
        self.runtime_context = runtime_context
        self.records: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.failure: str | None = None
        self.visited = {self.fetch["inputs"]["url"]}
        self.runtime_context.pages_used = 0
        self.runtime_context.requests_used = 0
        self.runtime_context.bytes_used = 0

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        crawler.signals.connect(spider._closed, signal=signals.spider_closed)
        crawler.signals.connect(spider._spider_error, signal=signals.spider_error)
        crawler.signals.connect(spider._item_error, signal=signals.item_error)
        return spider

    async def start(self):
        _verify_scrapy_tables(self, profile="live")
        yield Request(
            self.fetch["inputs"]["url"],
            callback=self.parse_page,
            errback=self._failed,
            cb_kwargs={"page_index": 0},
            headers={
                "Accept": "text/html"
                if self.select["operator_type"] == "select.html_records"
                else "application/json"
            },
        )

    def parse_page(self, response, page_index: int):
        _verify_scrapy_tables(self, profile="live")
        if response.status != 200:
            try:
                error_code = json.loads(response.body).get("error", "unknown")
            except (ValueError, AttributeError):
                error_code = "unknown"
            raise RuntimeFailure(
                "GATEWAY_FAILURE",
                f"gateway response status {response.status}: {error_code}",
            )
        if len(response.body) > self.budgets["max_response_bytes"]:
            raise RuntimeFailure(
                "PLAYBOOK_RESPONSE_BUDGET_EXCEEDED", "response exceeds budget"
            )
        digest = hashlib.sha256(response.body).hexdigest()
        headers = response.headers
        expected_digest = headers.get(b"x-mozaic-body-sha256", b"").decode("ascii")
        fetch_id = headers.get(b"x-mozaic-fetch-id", b"").decode("ascii")
        evidence_ref = headers.get(b"x-mozaic-evidence-ref", b"").decode("ascii")
        acquired_at = headers.get(b"x-mozaic-acquired-at", b"").decode("ascii")
        media_type = (
            headers.get(b"x-mozaic-media-type", b"")
            .decode("ascii")
            .split(";", 1)[0]
            .lower()
        )
        allowed_media = (
            {"text/html"}
            if self.select["operator_type"] == "select.html_records"
            else {"application/json", "application/*+json"}
        )
        if (
            "sha256:" + digest != expected_digest
            or not fetch_id
            or not evidence_ref
            or media_type not in allowed_media
        ):
            raise RuntimeFailure(
                "GATEWAY_EVIDENCE_INVALID", "gateway evidence metadata differs"
            )
        acquired_datetime = datetime.fromisoformat(
            acquired_at.replace("Z", "+00:00")  # noqa: FURB162
        )
        if acquired_datetime.tzinfo is None or acquired_datetime.utcoffset() is None:
            raise RuntimeFailure(
                "GATEWAY_EVIDENCE_INVALID", "acquisition timestamp needs a timezone"
            )
        document = {
            "url": response.url,
            "requested_url": response.request.url,
            "body": response.body,
            "sha256": digest,
            "evidence_ref": evidence_ref,
            "content_type": media_type,
            "status": response.status,
            "page_index": page_index,
        }
        self.documents.append(document)
        if self.select["operator_type"] == "select.html_records":
            selected = reference_runtime.select_html_records(
                self.runtime_context,
                document,
                selector=self.select["inputs"]["selector"],
                cardinality=self.select["inputs"]["cardinality"],
                max_matches=self.select["inputs"]["max_matches"],
                evidence_refs=[evidence_ref],
            )
        else:
            selected = reference_runtime.select_json_records(
                self.runtime_context,
                document,
                array_path=self.select["inputs"]["array_path"],
                max_matches=self.select["inputs"]["max_matches"],
                evidence_refs=[evidence_ref],
            )
        extracted = reference_runtime.extract_fields(
            self.runtime_context,
            selected,
            fields=self.extract["inputs"]["fields"],
            operation_id=self.extract["operation_id"],
        )
        if len(self.records) + len(extracted) > self.budgets["max_records"]:
            raise RuntimeFailure(
                "PLAYBOOK_RECORD_BUDGET_EXCEEDED", "record budget exceeded"
            )
        self.records.extend(extracted)
        self.runtime_context.pages_used += 1
        self.runtime_context.requests_used += 1
        self.runtime_context.bytes_used += len(response.body)
        self.evidence.append(
            {
                "fetch_id": fetch_id,
                "evidence_ref": evidence_ref,
                "source_url": response.url,
                "status": response.status,
                "acquired_at": acquired_at,
                "media_type": media_type,
                "body_sha256": digest,
                "body_size_bytes": len(response.body),
                "body_representation": headers.get(
                    b"x-mozaic-body-representation", b""
                ).decode("ascii"),
            }
        )
        if self.discover is None:
            return
        discovered = reference_runtime.discover_links(
            self.runtime_context,
            document,
            selector=self.discover["inputs"]["selector"],
            max_links=self.discover["inputs"]["max_links"],
            evidence_refs=[evidence_ref],
        )
        candidates = discovered["urls"]
        next_url = next((url for url in candidates if url not in self.visited), None)
        if next_url is None and any(url in self.visited for url in candidates):
            raise RuntimeFailure("PAGINATION_LOOP", "pagination revisited a page")
        if next_url is not None and page_index + 1 < self.max_pages:
            self.visited.add(next_url)
            yield Request(
                next_url,
                callback=self.parse_page,
                errback=self._failed,
                cb_kwargs={"page_index": page_index + 1},
                headers={"Accept": "text/html"},
            )
        elif next_url is not None and self.completion_mode == "bounded_window":
            self.pagination_termination = "planned_page_limit"
        elif next_url is not None:
            raise RuntimeFailure(
                "PAGINATION_UNEXPECTED_BUDGET_EXHAUSTED",
                "natural pagination exhausted its page budget",
            )
        else:
            self.pagination_termination = "natural_end"

    def _failed(self, failure):
        self.failure = failure.getErrorMessage()

    def _spider_error(self, failure, response, spider):
        self.failure = failure.getErrorMessage()

    def _item_error(self, item, response, spider, failure):
        self.failure = failure.getErrorMessage()

    def _closed(self, spider, reason: str):
        if reason != "finished" and self.failure is None:
            self.failure = "EXECUTION_CANCELLED"

    def close_runtime(self):
        shutil.rmtree(self.fixture_root, ignore_errors=True)
        shutil.rmtree(self.context_root, ignore_errors=True)


def _fixture_plan(plan: dict[str, Any]) -> None:
    required = {
        "profile",
        "source",
        "budgets",
        "fixtures",
        "fetch",
        "select",
        "extract",
        "discover",
        "page_limit",
        "raw_output",
    }
    if set(plan) != required or plan.get("profile") != "scrapy-fixture-v1":
        raise RuntimeFailure(
            "RUNTIME_PLAN_INVALID", "fixture plan shape/profile differs"
        )
    if not isinstance(plan["fixtures"], list) or not plan["fixtures"]:
        raise RuntimeFailure("RUNTIME_PLAN_INVALID", "fixture identities are missing")


def run_fixture(
    plan: dict[str, Any], identity: dict[str, Any], argv: list[str] | None = None
) -> int:
    """Run one verified fixture BotBuild and publish its bounded result bundle."""
    _verify_runtime(identity)
    _fixture_plan(plan)
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = _output_path(args.output)
    settings = Settings()
    if os.environ.get("SCRAPY_SETTINGS_MODULE"):
        settings.setmodule(os.environ["SCRAPY_SETTINGS_MODULE"], priority="project")
    settings.setdict(scrapy_fixture_runtime.handler_settings(), priority="cmdline")
    process = CrawlerProcess(settings=settings)
    crawler = process.create_crawler(FixturePlanSpider)
    process.crawl(
        crawler, plan=plan, fixture_root=args.fixtures, output_root=args.output
    )
    spider = None
    try:
        process.start(stop_after_crawl=True)
        spider = crawler.spider
        if spider is None or spider.failure is not None:
            print(
                json.dumps(
                    {
                        "state": "failed",
                        "diagnostic": None if spider is None else spider.failure,
                    },
                    sort_keys=True,
                )
            )
            return 1
        report = scrapy_fixture_runtime.publish_output(
            spider,
            output,
            engine={
                "id": "scrapy",
                "version": __import__("scrapy").__version__,
                "backend": "scrapy-fixture",
            },
        )
    except (RuntimeFailure, OSError, ValueError, TypeError) as error:
        print(str(error))
        return 1
    finally:
        active_spider = spider or crawler.spider
        if active_spider is not None:
            shutil.rmtree(active_spider.context_root, ignore_errors=True)
    print(json.dumps(report, sort_keys=True))
    return 0


def _live_plan(plan: dict[str, Any]) -> None:
    required = {
        "profile",
        "source",
        "budgets",
        "fetch",
        "select",
        "extract",
        "discover",
        "pagination",
    }
    if set(plan) != required or plan.get("profile") != "scrapy-live-http-v1":
        raise RuntimeFailure("RUNTIME_PLAN_INVALID", "live plan shape/profile differs")


def run_live(
    plan: dict[str, Any], identity: dict[str, Any], argv: list[str] | None = None
) -> int:
    """Run one verified live BotBuild through its delegated gateway transport."""
    _verify_runtime(identity, live=True)
    _live_plan(plan)
    from . import scrapy_live_runtime

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = _output_path(args.output)
    settings = Settings()
    settings.setdict(scrapy_live_runtime.live_handler_settings(), priority="cmdline")
    if plan["pagination"] is not None:
        settings.set("DOWNLOAD_DELAY", 1.1, priority="cmdline")
        settings.set("RANDOMIZE_DOWNLOAD_DELAY", False, priority="cmdline")
    process = CrawlerProcess(settings=settings)
    crawler = process.create_crawler(LivePlanSpider)
    process.crawl(crawler, plan=plan, output_root=args.output)
    spider = None
    try:
        process.start(stop_after_crawl=True)
        spider = crawler.spider
        if (
            spider is None
            or spider.failure is not None
            or spider.runtime_context.pages_used < 1
            or len(spider.evidence) != spider.runtime_context.pages_used
        ):
            print(
                json.dumps(
                    {
                        "state": "failed",
                        "diagnostic": None if spider is None else spider.failure,
                    },
                    sort_keys=True,
                )
            )
            return 1
        report = reference_runtime.write_records(spider.runtime_context, spider.records)
        if spider.pagination is not None:
            report["pagination_termination"] = getattr(
                spider, "pagination_termination", None
            )
        reference_runtime.write_report(spider.runtime_context, report)
        reference_runtime.write_raw_references(spider.runtime_context, spider.documents)
        evidence_lines = b"".join(
            (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
            for row in spider.evidence
        )
        with (output / "fetch-evidence.jsonl").open("xb") as stream:
            stream.write(evidence_lines)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (RuntimeFailure, OSError, ValueError, TypeError) as error:
        print(str(error))
        return 1
    finally:
        active_spider = spider or crawler.spider
        if active_spider is not None:
            active_spider.close_runtime()
