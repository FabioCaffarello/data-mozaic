"""Deterministic first live Scrapy backend, delegated through the gateway."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any

from mozaic_bot_build import derive_bot_id

from .backend_python_http import _platform_slug, _safe_output_path
from .backend_python_http import verify_build as _verify_build
from .playbook_v2 import IR_VERSION, TypedIR, canonical_json
from .reference_runtime import RUNTIME_VERSION
from .runtime_identity import runtime_bundle_files
from .scrapy_live_runtime import LIVE_RUNTIME_VERSION
from .syntax import PlaybookError

BACKEND_ID = "scrapy-live-http"
BACKEND_VERSION = "1.0.0"
COMPILER_VERSION = "mozaic-collector-engineering/0.3.0"
PROFILE = json.loads(
    files("collector_engineering")
    .joinpath("profiles/scrapy-live-http-v1.json")
    .read_text(encoding="utf-8")
)


def _supported_graph(
    ir: TypedIR,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    nodes = ir.document["nodes"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        by_type.setdefault(node["operator_type"], []).append(node)
    if (
        len(by_type.get("http.fetch", [])) != 1
        or len(by_type.get("select.json_records", []))
        + len(by_type.get("select.html_records", []))
        != 1
        or len(by_type.get("extract.fields", [])) != 1
        or len(by_type.get("output.records", [])) != 1
        or len(by_type.get("output.raw", [])) > 1
        or len(by_type.get("links.discover", [])) not in {0, 1}
        or len(by_type.get("pagination.bounded", [])) not in {0, 1}
        or len(by_type.get("links.discover", []))
        != len(by_type.get("pagination.bounded", []))
        or len(nodes) not in {4, 5, 6, 7}
    ):
        raise PlaybookError(
            "[LIVE_GRAPH_UNSUPPORTED] live v1 requires one GET, JSON or HTML selection, extraction and records output"
        )
    fetch = by_type["http.fetch"][0]
    select = (by_type.get("select.json_records") or by_type.get("select.html_records"))[
        0
    ]
    extract = by_type["extract.fields"][0]
    output = by_type["output.records"][0]
    discover = by_type.get("links.discover", [None])[0]
    pagination = by_type.get("pagination.bounded", [None])[0]
    selector_input = select["inputs"].get(
        "document", select["inputs"].get("documents", {})
    )
    selector_upstream = pagination or fetch
    if (
        selector_input.get("operation") != selector_upstream["operation_id"]
        or extract["inputs"].get("records", {}).get("operation")
        != select["operation_id"]
        or output["inputs"].get("records", {}).get("operation")
        != extract["operation_id"]
        or (
            by_type.get("output.raw")
            and by_type["output.raw"][0]["inputs"].get("documents", {}).get("operation")
            != selector_upstream["operation_id"]
        )
        or (
            discover is not None
            and discover["inputs"].get("document", {}).get("operation")
            != fetch["operation_id"]
        )
        or (
            pagination is not None
            and pagination["inputs"].get("links", {}).get("operation")
            != discover["operation_id"]
        )
    ):
        raise PlaybookError("[LIVE_GRAPH_UNSUPPORTED] live v1 requires a linear graph")
    if pagination is not None:
        page_limit = pagination["inputs"].get("max_pages", 0)
        if (
            select["operator_type"] != "select.html_records"
            or not pagination["inputs"].get("stop_when_no_links")
            or page_limit < 1
            or page_limit > ir.document["budgets"].get("max_pages", 0)
            or page_limit > ir.document["budgets"].get("max_requests", 0)
        ):
            raise PlaybookError(
                "[LIVE_PAGINATION_INVALID] live pagination requires HTML, a planned stop condition and page/request budgets"
            )
    content_type = fetch["inputs"].get("content_type")
    selected_type = select["operator_type"]
    if (
        selected_type == "select.json_records"
        and content_type not in {"application/json", "application/*+json"}
    ) or (selected_type == "select.html_records" and content_type != "text/html"):
        raise PlaybookError(
            "[LIVE_MEDIA_TYPE_UNSUPPORTED] selector and fetch media type differ"
        )
    if ir.document["budgets"].get("max_requests", 0) < 1:
        raise PlaybookError("[LIVE_BUDGET_INVALID] max_requests must be positive")
    return fetch, select, extract, discover, pagination


def generate_bot(ir: TypedIR, runtime_identity: dict[str, Any]) -> bytes:
    fetch, select, extract, discover, pagination = _supported_graph(ir)
    html = select["operator_type"] == "select.html_records"
    lines = [
        '"""Generated live Scrapy bot. All source HTTP is delegated to the gateway."""',
        "import argparse, hashlib, json, os, sys, tempfile, shutil",
        "from datetime import datetime",
        "from pathlib import Path",
        "from scrapy import Request, Spider, signals",
        "from scrapy.crawler import CrawlerProcess",
        "from scrapy.settings import Settings",
        "sys.path.insert(0, str(Path(__file__).resolve().parent / 'runtime'))",
        "from mozaic_bot_runtime import reference_runtime, scrapy_live_runtime",
        "from mozaic_bot_runtime.identity import verify as verify_runtime",
        "RUNTIME_ROOT = (Path(__file__).resolve().parent / 'runtime' / 'mozaic_bot_runtime').resolve(strict=True)",
        "if Path(reference_runtime.__file__).resolve().parent != RUNTIME_ROOT or Path(scrapy_live_runtime.__file__).resolve().parent != RUNTIME_ROOT: raise RuntimeError('[BUILD_RUNTIME_IMPORT_MISMATCH]')",
        f"RUNTIME_IDENTITY = {runtime_identity!r}",
        "verify_runtime(RUNTIME_ROOT, RUNTIME_IDENTITY['files'], RUNTIME_IDENTITY['sha256'])",
        f"SOURCE = {ir.document['source']!r}",
        f"BUDGETS = {ir.document['budgets']!r}",
        f"START_URL = {fetch['inputs']['url']!r}",
        f"SELECT = {select!r}",
        f"EXTRACT = {extract!r}",
        f"DISCOVER = {discover!r}",
        f"PAGINATION = {pagination!r}",
        "MAX_PAGES = BUDGETS['max_pages'] if PAGINATION is None else PAGINATION['inputs']['max_pages']",
        "PAGINATION_COMPLETION_MODE = 'natural_end' if PAGINATION is None else PAGINATION['inputs'].get('completion_mode', 'natural_end')",
        "class GeneratedLiveSpider(Spider):",
        "    name = 'mozaic-generated-live-json'",
        "    def __init__(self, output_root, **kwargs):",
        "        super().__init__(**kwargs)",
        "        self.output_root = Path(output_root).absolute()",
        "        self.fixture_root = Path(tempfile.mkdtemp(prefix='dm-live-empty-fixtures-'))",
        "        self.context_root = Path(tempfile.mkdtemp(prefix='dm-live-context-'))",
        "        self.ctx = reference_runtime.RuntimeContext.create(str(self.fixture_root), str(self.output_root), SOURCE, BUDGETS, [])",
        "        self.records = []; self.evidence = []; self.documents = []; self.failure = None; self.page_urls = [START_URL]; self.visited = {START_URL}; self.ctx.pages_used = 0; self.ctx.requests_used = 0; self.ctx.bytes_used = 0",
        "    @classmethod",
        "    def from_crawler(cls, crawler, *args, **kwargs):",
        "        spider = super().from_crawler(crawler, *args, **kwargs)",
        "        crawler.signals.connect(spider._closed, signal=signals.spider_closed)",
        "        crawler.signals.connect(spider._spider_error, signal=signals.spider_error)",
        "        crawler.signals.connect(spider._item_error, signal=signals.item_error)",
        "        return spider",
        "    def _verify_handlers(self):",
        "        handlers = self.crawler.engine.downloader.handlers",
        "        expected = 'mozaic_bot_runtime.scrapy_live_runtime.GatewayDownloadHandler'",
        "        if set(handlers._paths) != {'http', 'https'} or any(handlers._paths[s] != expected for s in ('http','https')): raise RuntimeError('[HANDLER_TABLE_UNSAFE]')",
        "        middleware = self.crawler.engine.downloader.middleware.middlewares",
        "        if len(middleware) != 1 or type(middleware[0]).__name__ != 'LiveRequestGuard': raise RuntimeError('[MIDDLEWARE_TABLE_UNSAFE]')",
        "    async def start(self):",
        "        self._verify_handlers()",
        "        yield Request(START_URL, callback=self.parse_page, errback=self._failed, cb_kwargs={'page_index': 0}, headers={'Accept': 'text/html' if SELECT['operator_type'] == 'select.html_records' else 'application/json'})",
        "    def parse_page(self, response, page_index):",
        "        self._verify_handlers()",
        "        if response.status != 200:",
        "            try: error_code = json.loads(response.body).get('error', 'unknown')",
        "            except (ValueError, AttributeError): error_code = 'unknown'",
        "            raise RuntimeError('[GATEWAY_FAILURE:' + str(response.status) + ':' + str(error_code) + ']')",
        "        if len(response.body) > BUDGETS['max_response_bytes']: raise RuntimeError('[PLAYBOOK_RESPONSE_BUDGET_EXCEEDED]')",
        "        digest = hashlib.sha256(response.body).hexdigest()",
        "        expected_digest = response.headers.get(b'x-mozaic-body-sha256', b'').decode('ascii')",
        "        fetch_id = response.headers.get(b'x-mozaic-fetch-id', b'').decode('ascii')",
        "        evidence_ref = response.headers.get(b'x-mozaic-evidence-ref', b'').decode('ascii')",
        "        acquired_at = response.headers.get(b'x-mozaic-acquired-at', b'').decode('ascii')",
        "        media_type = response.headers.get(b'x-mozaic-media-type', b'').decode('ascii').split(';', 1)[0].lower()",
        f"        allowed_media = {{'text/html'}} if {html!r} else {{'application/json','application/*+json'}}",
        "        if 'sha256:' + digest != expected_digest or not fetch_id or not evidence_ref or media_type not in allowed_media: raise RuntimeError('[GATEWAY_EVIDENCE_INVALID]')",
        "        datetime.fromisoformat(acquired_at.replace('Z', '+00:00'))",
        "        document = {'url': response.url, 'requested_url': response.request.url, 'body': response.body, 'sha256': digest, 'evidence_ref': evidence_ref, 'content_type': media_type, 'status': response.status, 'page_index': page_index}",
        "        self.documents.append(document)",
        "        if SELECT['operator_type'] == 'select.html_records': selected = reference_runtime.select_html_records(self.ctx, document, selector=SELECT['inputs']['selector'], cardinality=SELECT['inputs']['cardinality'], max_matches=SELECT['inputs']['max_matches'], evidence_refs=[evidence_ref])",
        "        else: selected = reference_runtime.select_json_records(self.ctx, document, array_path=SELECT['inputs']['array_path'], max_matches=SELECT['inputs']['max_matches'], evidence_refs=[evidence_ref])",
        "        extracted = reference_runtime.extract_fields(self.ctx, selected, fields=EXTRACT['inputs']['fields'], operation_id=EXTRACT['operation_id'])",
        "        if len(self.records) + len(extracted) > BUDGETS['max_records']: raise RuntimeError('[PLAYBOOK_RECORD_BUDGET_EXCEEDED]')",
        "        self.records.extend(extracted)",
        "        self.ctx.pages_used += 1; self.ctx.requests_used += 1; self.ctx.bytes_used += len(response.body)",
        "        self.evidence.append({'fetch_id': fetch_id, 'evidence_ref': evidence_ref, 'source_url': response.url, 'status': response.status, 'acquired_at': acquired_at, 'media_type': media_type, 'body_sha256': digest, 'body_size_bytes': len(response.body), 'body_representation': response.headers.get(b'x-mozaic-body-representation', b'').decode('ascii')})",
        "        if DISCOVER is not None:",
        "            discovered = reference_runtime.discover_links(self.ctx, document, selector=DISCOVER['inputs']['selector'], max_links=DISCOVER['inputs']['max_links'], evidence_refs=[evidence_ref])",
        "            candidates = discovered['urls']",
        "            next_url = next((url for url in candidates if url not in self.visited), None)",
        "            if next_url is None and any(url in self.visited for url in candidates): raise RuntimeError('[PAGINATION_LOOP]')",
        "            if next_url is not None and page_index + 1 < MAX_PAGES:",
        "                self.visited.add(next_url); self.page_urls.append(next_url)",
        "                yield Request(next_url, callback=self.parse_page, errback=self._failed, cb_kwargs={'page_index': page_index + 1}, headers={'Accept': 'text/html'})",
        "            elif next_url is not None and PAGINATION_COMPLETION_MODE == 'bounded_window': self.pagination_termination = 'planned_page_limit'",
        "            elif next_url is not None: raise RuntimeError('[PAGINATION_UNEXPECTED_BUDGET_EXHAUSTED]')",
        "            else: self.pagination_termination = 'natural_end'",
        "    def _failed(self, failure): self.failure = failure.getErrorMessage()",
        "    def _spider_error(self, failure, response, spider): self.failure = failure.getErrorMessage()",
        "    def _item_error(self, item, response, spider, failure): self.failure = failure.getErrorMessage()",
        "    def _closed(self, spider, reason):",
        "        if reason not in {'finished'} and self.failure is None: self.failure = 'EXECUTION_CANCELLED'",
        "    def close_runtime(self):",
        "        shutil.rmtree(self.fixture_root, ignore_errors=True); shutil.rmtree(self.context_root, ignore_errors=True)",
        "def main(argv=None):",
        "    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True); args = parser.parse_args(argv)",
        "    output = Path(args.output).absolute()",
        "    if output.exists() or output.parent.is_symlink() or not output.parent.is_dir(): raise RuntimeError('[OUTPUT_PATH_UNSAFE]')",
        "    settings = Settings(); settings.setdict(scrapy_live_runtime.live_handler_settings(), priority='cmdline')",
        "    if PAGINATION is not None: settings.set('DOWNLOAD_DELAY', 1.1, priority='cmdline'); settings.set('RANDOMIZE_DOWNLOAD_DELAY', False, priority='cmdline')",
        "    process = CrawlerProcess(settings=settings); crawler = process.create_crawler(GeneratedLiveSpider); process.crawl(crawler, output_root=args.output); process.start(stop_after_crawl=True)",
        "    spider = crawler.spider",
        "    if spider is None or spider.failure is not None or spider.ctx.pages_used < 1 or len(spider.evidence) != spider.ctx.pages_used:",
        "        if spider is not None: spider.close_runtime()",
        "        print(json.dumps({'state':'failed','diagnostic': None if spider is None else spider.failure}, sort_keys=True)); return 1",
        "    try:",
        "        report = reference_runtime.write_records(spider.ctx, spider.records)",
        "        if PAGINATION is not None: report['pagination_termination'] = getattr(spider, 'pagination_termination', None)",
        "        reference_runtime.write_report(spider.ctx, report)",
        "        reference_runtime.write_raw_references(spider.ctx, spider.documents)",
        "        (output / 'fetch-evidence.jsonl').write_bytes(b''.join((json.dumps(row, sort_keys=True, separators=(',', ':'))+'\\n').encode() for row in spider.evidence))",
        "        print(json.dumps(report, sort_keys=True)); return 0",
        "    finally: spider.close_runtime()",
        "if __name__ == '__main__': raise SystemExit(main())",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def compile_build(
    ir: TypedIR,
    *,
    output: Path,
    lockfile: Path,
    playbook_sha256: str | None = None,
    target_platform: str | None = None,
) -> dict[str, Any]:
    if ir.document.get("ir_version") != IR_VERSION:
        raise PlaybookError(
            "[IR_VERSION_UNSUPPORTED] live backend requires collector-ir/v1"
        )
    fetch, _, _, _, _ = _supported_graph(ir)
    if "http.fetch.fixture" not in ir.document["capabilities"]:
        raise PlaybookError(
            "[LIVE_PLAYBOOK_UNSUPPORTED] source graph must be authored from the current fetch operator"
        )
    target_platform = target_platform or _platform_slug()
    if "/" not in target_platform:
        raise PlaybookError("[TARGET_PLATFORM_INVALID] target platform is invalid")
    runtime_files, runtime_identity = runtime_bundle_files(
        ("reference_runtime.py", "syntax.py", "scrapy_live_runtime.py")
    )
    files_map = {
        "bot.py": generate_bot(ir, runtime_identity),
        "capability-profile.json": canonical_json(PROFILE),
        "README.md": b"# Generated Scrapy live HTTP bot\n\nThis build delegates every source request to the authenticated acquisition gateway. Admission and policy authorization are required at runtime.\n",
    }
    files_map.update(runtime_files)
    generated = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(files_map.items())
    }
    manifest_core = {
        "manifest_version": "bot-build-manifest/v2",
        "playbook_id": ir.document["playbook_id"],
        "bot_id": derive_bot_id(
            ir.document["playbook_id"], ir.document["source"]["source_id"]
        ),
        "source": {
            "source_id": ir.document["source"]["source_id"],
            "source_revision": ir.document["source"]["revision"],
        },
        "playbook_version": "capture-playbook-draft/v2",
        "playbook_sha256": playbook_sha256,
        "ir_version": IR_VERSION,
        "ir_sha256": ir.sha256,
        "backend_id": BACKEND_ID,
        "backend_version": BACKEND_VERSION,
        "capability_profile_sha256": hashlib.sha256(
            canonical_json(PROFILE)
        ).hexdigest(),
        "capability_profile_id": "scrapy-live-http-v1",
        "compiler_version": COMPILER_VERSION,
        "runtime_identity": runtime_identity,
        "dependency_identity": {
            "lockfile_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
            "packages": {
                "scrapy": importlib.metadata.version("scrapy"),
                "selectolax": "0.4.13",
            },
        },
        "compatibility": {
            "worker_contract": "scrapy-collector/v1",
            "execution_mode": "live-authorized",
            "runtime_profile": "scrapy-live-http-v1",
            "python_minor": f"{platform.python_version_tuple()[0]}.{platform.python_version_tuple()[1]}",
            "target_platform": target_platform,
            "scrapy": importlib.metadata.version("scrapy"),
            "scrapy_runtime": LIVE_RUNTIME_VERSION,
        },
        "toolchain": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform_system": platform.system(),
            "platform_machine": platform.machine(),
            "runtime": LIVE_RUNTIME_VERSION,
            "reference_runtime": RUNTIME_VERSION,
            "scrapy": importlib.metadata.version("scrapy"),
            "selectolax": "0.4.13",
        },
        "dependency_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        "generated_files": generated,
        "input_fixtures": [],
        "live_request": {"method": "GET", "url": fetch["inputs"]["url"]},
    }
    manifest = {
        **manifest_core,
        "build_id": hashlib.sha256(canonical_json(manifest_core)).hexdigest(),
    }
    destination = _safe_output_path(output)
    destination.mkdir(parents=True, mode=0o700)
    try:
        for name, data in files_map.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as stream:
                stream.write(data)
        with (destination / "manifest.json").open("xb") as stream:
            stream.write(
                json.dumps(manifest, sort_keys=True, indent=2).encode() + b"\n"
            )
    except BaseException:
        shutil.rmtree(destination)
        raise
    return manifest


def verify_build(directory: Path) -> dict[str, Any]:
    version = importlib.metadata.version("scrapy")
    manifest = _verify_build(
        directory,
        expected_backend_id=BACKEND_ID,
        expected_backend_version=BACKEND_VERSION,
        expected_compiler_version=COMPILER_VERSION,
        profile=PROFILE,
        expected_toolchain={
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform_system": platform.system(),
            "platform_machine": platform.machine(),
            "runtime": LIVE_RUNTIME_VERSION,
            "reference_runtime": RUNTIME_VERSION,
            "scrapy": version,
            "selectolax": "0.4.13",
        },
    )
    compatibility = manifest.get("compatibility", {})
    if (
        manifest.get("capability_profile_id") != "scrapy-live-http-v1"
        or compatibility.get("execution_mode") != "live-authorized"
        or compatibility.get("runtime_profile") != "scrapy-live-http-v1"
        or manifest.get("input_fixtures") != []
    ):
        raise PlaybookError(
            "[BUILD_EXECUTION_MODE_UNSUPPORTED] live build binding differs"
        )
    return manifest
