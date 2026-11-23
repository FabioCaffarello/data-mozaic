"""Deterministic compiler for the Scrapy fixture-only backend."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
from importlib.resources import files
from pathlib import Path
from typing import Any

from .backend_python_http import _safe_output_path
from .backend_python_http import verify_build as _verify_build
from .fixture_evidence import inspect_fixture_evidence
from .playbook_v2 import IR_VERSION, TypedIR, canonical_json
from .reference_runtime import RUNTIME_VERSION
from .runtime_identity import runtime_bundle_files
from .scrapy_fixture_runtime import SCRAPY_RUNTIME_VERSION
from .syntax import PlaybookError

BACKEND_ID = "scrapy-fixture"
BACKEND_VERSION = "1.0.0"
COMPILER_VERSION = "mozaic-collector-engineering/0.3.0"
PROFILE = json.loads(
    files("collector_engineering")
    .joinpath("profiles/scrapy-fixture-v1.json")
    .read_text(encoding="utf-8")
)
BACKEND_CAPABILITIES = frozenset(PROFILE["capabilities"])


def _callback_name(role: str, operation_id: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9_]", "_", operation_id)[:32] or "operation"
    suffix = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:8]
    return f"_op_{role}_{readable}_{suffix}"


def check_capabilities(ir: TypedIR) -> list[dict[str, str]]:
    missing = sorted(set(ir.document["capabilities"]) - BACKEND_CAPABILITIES)
    return [
        {
            "code": "CAPABILITY_UNSUPPORTED",
            "capability": capability,
            "backend": BACKEND_ID,
            "message": f"backend {BACKEND_ID} does not support {capability}",
        }
        for capability in missing
    ]


def _supported_graph(
    ir: TypedIR,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    nodes = ir.document["nodes"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        name = f"{node['operator_type']}/{node['operator_version']}"
        by_type.setdefault(name, []).append(node)
    fetches = by_type.get("http.fetch/v1", [])
    selections = by_type.get("select.html_records/v1", []) + by_type.get(
        "select.json_records/v1", []
    )
    extracts = by_type.get("extract.fields/v1", [])
    record_outputs = by_type.get("output.records/v1", [])
    if (
        len(fetches) != 1
        or len(selections) != 1
        or len(extracts) != 1
        or len(record_outputs) != 1
        or len(by_type.get("browser.render_dom/v1", []))
        or len(by_type.get("select.rendered_dom_records/v1", []))
    ):
        raise PlaybookError(
            "[BACKEND_GRAPH_UNSUPPORTED] Scrapy fixture backend requires one fetch, one selection, one extraction and one record output"
        )
    discover = by_type.get("links.discover/v1", [])
    paginate = by_type.get("pagination.bounded/v1", [])
    if len(discover) > 1 or len(paginate) > 1 or bool(discover) != bool(paginate):
        raise PlaybookError(
            "[BACKEND_GRAPH_UNSUPPORTED] Scrapy fixture backend supports one bounded discovery and pagination pair"
        )
    allowed = {
        "http.fetch/v1",
        "links.discover/v1",
        "pagination.bounded/v1",
        "select.html_records/v1",
        "select.json_records/v1",
        "extract.fields/v1",
        "output.records/v1",
        "output.raw/v1",
    }
    unsupported = sorted(set(by_type) - allowed)
    if unsupported:
        raise PlaybookError(
            f"[BACKEND_GRAPH_UNSUPPORTED] unsupported operation graph: {', '.join(unsupported)}"
        )
    fetch, select, extract = fetches[0], selections[0], extracts[0]
    if discover:
        if (
            discover[0]["inputs"]["document"]["operation"] != fetch["operation_id"]
            or paginate[0]["inputs"]["links"]["operation"]
            != discover[0]["operation_id"]
            or select["inputs"]["documents"]["operation"] != paginate[0]["operation_id"]
        ):
            raise PlaybookError(
                "[BACKEND_GRAPH_UNSUPPORTED] pagination bindings do not match the supported linear graph"
            )
    else:
        selection_binding = select["inputs"].get("document") or select["inputs"].get(
            "documents"
        )
        if selection_binding["operation"] != fetch["operation_id"]:
            raise PlaybookError(
                "[BACKEND_GRAPH_UNSUPPORTED] selection must bind to the single fetch operation"
            )
    if extract["inputs"]["records"]["operation"] != select["operation_id"]:
        raise PlaybookError(
            "[BACKEND_GRAPH_UNSUPPORTED] extraction must bind to the single selection operation"
        )
    if record_outputs[0]["inputs"]["records"]["operation"] != extract["operation_id"]:
        raise PlaybookError(
            "[BACKEND_GRAPH_UNSUPPORTED] output must bind to the single extraction operation"
        )
    return fetch, select, discover[0] if discover else None


def generate_spider(
    ir: TypedIR,
    fixtures: list[dict[str, Any]],
    runtime_identity: dict[str, Any],
) -> bytes:
    fetch, select, discover = _supported_graph(ir)
    extract = next(
        node
        for node in ir.document["nodes"]
        if node["operator_type"] == "extract.fields"
    )
    raw_output = any(
        node["operator_type"] == "output.raw" for node in ir.document["nodes"]
    )
    page_limit = ir.document["budgets"]["max_pages"]
    start_callback = _callback_name("fetch", fetch["operation_id"])
    page_callback = _callback_name("select", select["operation_id"])
    if discover:
        page_limit = min(
            page_limit,
            next(
                node
                for node in ir.document["nodes"]
                if node["operator_type"] == "pagination.bounded"
            )["inputs"]["max_pages"],
        )
    if page_limit > 200:
        raise PlaybookError(
            "[BACKEND_BUDGET_UNSUPPORTED] page limit exceeds Scrapy fixture bound"
        )

    lines = [
        '"""Generated Scrapy fixture bot; operations are compiled from collector-ir/v1."""',
        "import argparse",
        "import json",
        "import sys",
        "import tempfile",
        "import shutil",
        "from pathlib import Path",
        "from scrapy import Request, Spider",
        "from scrapy.crawler import CrawlerProcess",
        "from scrapy.settings import Settings",
        "sys.path.insert(0, str(Path(__file__).resolve().parent / 'runtime'))",
        "from mozaic_bot_runtime import reference_runtime, scrapy_fixture_runtime",
        "from mozaic_bot_runtime.identity import verify as verify_runtime",
        "RUNTIME_ROOT = (Path(__file__).resolve().parent / 'runtime' / 'mozaic_bot_runtime').resolve(strict=True)",
        "if Path(reference_runtime.__file__).resolve().parent != RUNTIME_ROOT or Path(scrapy_fixture_runtime.__file__).resolve().parent != RUNTIME_ROOT:",
        "    raise RuntimeError('[BUILD_RUNTIME_IMPORT_MISMATCH] engine runtime was not imported from this BotBuild')",
        f"RUNTIME_IDENTITY = {runtime_identity!r}",
        "verify_runtime(RUNTIME_ROOT, RUNTIME_IDENTITY['files'], RUNTIME_IDENTITY['sha256'])",
        f"SOURCE = {ir.document['source']!r}",
        f"BUDGETS = {ir.document['budgets']!r}",
        f"EXPECTED_FIXTURES = {fixtures!r}",
        f"START_URL = {fetch['inputs']['url']!r}",
        f"FETCH_OPERATION_ID = {fetch['operation_id']!r}",
        f"SELECT_OPERATION_ID = {select['operation_id']!r}",
        f"EXTRACT_OPERATION_ID = {extract['operation_id']!r}",
        f"SELECT_OPERATION = {select!r}",
        f"EXTRACT_OPERATION = {extract!r}",
        f"DISCOVER_OPERATION = {discover!r}",
        f"PAGE_LIMIT = {page_limit!r}",
        f"RAW_OUTPUT = {raw_output!r}",
        "",
        "class GeneratedSpider(Spider, scrapy_fixture_runtime.GeneratedSpiderProtocol):",
        "    name = 'mozaic-generated-fixture-spider'",
        "",
        "    def __init__(self, fixture_root, output_root, **kwargs):",
        "        super().__init__(**kwargs)",
        "        self.fixture_root = Path(fixture_root).resolve(strict=True)",
        "        self.output_root = Path(output_root).absolute()",
        "        self.source = SOURCE",
        "        self.budgets = BUDGETS",
        "        self.fixture_by_url = {page['url']: page for page in EXPECTED_FIXTURES}",
        "        self.runtime_module = reference_runtime",
        "        self.context_root = Path(tempfile.mkdtemp(prefix='mozaic-scrapy-context-')).resolve()",
        "        self.runtime_context = reference_runtime.RuntimeContext.create(str(self.fixture_root), str(self.context_root), SOURCE, BUDGETS, EXPECTED_FIXTURES)",
        "        self.documents = []",
        "        self.records = []",
        "        self.buffered_output_bytes = 0",
        "        self.failure = None",
        "        self.selected_count = 0",
        "        self.raw_output = RAW_OUTPUT",
        "        self.discovered_urls = []",
        "        self.visited_urls = {START_URL}",
        "",
        "    @classmethod",
        "    def from_crawler(cls, crawler, *args, **kwargs):",
        "        spider = super().from_crawler(crawler, *args, **kwargs)",
        "        crawler.signals.connect(spider._spider_error, signal='spider_error')",
        "        crawler.signals.connect(spider._item_error, signal='item_error')",
        "        crawler.signals.connect(spider._spider_closed, signal='spider_closed')",
        "        return spider",
        "",
        "    def scope_check(self, url):",
        "        return reference_runtime._scoped(url, SOURCE)",
        "",
        "    def _verify_handler_table(self):",
        "        handlers = self.crawler.engine.downloader.handlers",
        "        expected = {'http', 'https'}",
        "        if set(handlers._paths) != expected or set(handlers._handlers) != expected:",
        "            raise RuntimeError('[HANDLER_TABLE_UNSAFE] effective handler table differs')",
        "        wanted = 'mozaic_bot_runtime.scrapy_fixture_runtime.FixtureOnlyDownloadHandler'",
        "        if any(handlers._paths[scheme] != wanted for scheme in expected):",
        "            raise RuntimeError('[HANDLER_TABLE_UNSAFE] non-fixture handler is configured')",
        "        if handlers._notconfigured:",
        "            raise RuntimeError('[HANDLER_TABLE_UNSAFE] handler table has unexpected schemes')",
        "        downloader_middleware = self.crawler.engine.downloader.middleware.middlewares",
        "        if len(downloader_middleware) != 1 or type(downloader_middleware[0]).__name__ != 'RequestGuardMiddleware':",
        "            raise RuntimeError('[MIDDLEWARE_TABLE_UNSAFE] unexpected downloader middleware is loaded')",
        "        spider_middleware = self.crawler.engine.scraper.spidermw.middlewares",
        "        if spider_middleware:",
        "            raise RuntimeError('[MIDDLEWARE_TABLE_UNSAFE] spider middleware is not allowed')",
        "        if self.crawler.extensions.middlewares:",
        "            raise RuntimeError('[EXTENSION_TABLE_UNSAFE] Scrapy extensions are not allowed')",
        "        item_pipelines = self.crawler.engine.scraper.itemproc.middlewares",
        "        if len(item_pipelines) != 1 or type(item_pipelines[0]).__name__ != 'BoundedRecordPipeline':",
        "            raise RuntimeError('[PIPELINE_TABLE_UNSAFE] bounded record pipeline is required')",
        "",
        "    async def start(self):",
        "        self._verify_handler_table()",
        f"        yield Request(START_URL, callback=self.{start_callback}, errback=self._failed, meta={{'dm_operation': FETCH_OPERATION_ID, 'dm_page_index': 0}})",
        "",
        "    def _document(self, response, page_index):",
        "        expected = self.fixture_by_url.get(response.url)",
        "        if expected is None:",
        "            raise RuntimeError('[FIXTURE_NOT_IN_BUILD] response URL is not in BotBuild')",
        "        digest = __import__('hashlib').sha256(response.body).hexdigest()",
        "        if digest != expected['sha256'] or len(response.body) != expected['size_bytes']:",
        "            raise RuntimeError('[FIXTURE_IDENTITY_MISMATCH] response bytes differ')",
        "        return {'url': response.url, 'requested_url': response.request.url, 'body': response.body, 'sha256': digest, 'evidence_ref': expected['evidence_ref'], 'content_type': expected['content_type'], 'status': response.status, 'page_index': page_index}",
        "",
        f"    def {start_callback}(self, response):",
        "        self._verify_handler_table()",
        "        document = self._document(response, 0)",
        "        self.documents.append(document)",
    ]
    if discover:
        links = discover["inputs"]
        lines.extend(
            [
                "        yield from self._process_document(document)",
                f"        link_set = reference_runtime.discover_links(self.runtime_context, document, selector={links['selector']!r}, max_links={links['max_links']!r}, evidence_refs={links['evidence_refs']!r})",
                "        urls = link_set['urls']",
                "        for url in urls:",
                "            if url in self.visited_urls:",
                "                continue",
                "            if len(self.documents) + len(self.discovered_urls) >= PAGE_LIMIT:",
                "                break",
                "            self.visited_urls.add(url)",
                "            self.discovered_urls.append(url)",
                "        for page_index, url in enumerate(self.discovered_urls, start=1):",
                f"            yield Request(url, callback=self.{page_callback}, errback=self._failed, meta={{'dm_operation': SELECT_OPERATION_ID, 'dm_page_index': page_index, 'dm_discovered_from': document['evidence_ref']}})",
            ]
        )
    else:
        lines.append("        yield from self._process_document(document)")
    lines.extend(
        [
            "",
            f"    def {page_callback}(self, response):",
            "        self._verify_handler_table()",
            "        page_index = response.meta['dm_page_index']",
            "        document = self._document(response, page_index)",
            "        self.documents.append(document)",
            "        yield from self._process_document(document)",
            "",
            "    def _process_document(self, document):",
        ]
    )
    select_inputs = select["inputs"]
    if select["operator_type"] == "select.html_records":
        select_expr = (
            "reference_runtime.select_html_records(self.runtime_context, document, "
            f"selector={select_inputs['selector']!r}, cardinality='many', "
            f"max_matches={select_inputs['max_matches']!r}, evidence_refs={select_inputs['evidence_refs']!r})"
        )
    else:
        select_expr = (
            "reference_runtime.select_json_records(self.runtime_context, document, "
            f"array_path={select_inputs['array_path']!r}, max_matches={select_inputs['max_matches']!r}, "
            f"evidence_refs={select_inputs['evidence_refs']!r})"
        )
    lines.extend(
        [
            f"        records = {select_expr}",
            "        self.selected_count += len(records)",
            "        if self.selected_count > SELECT_OPERATION['inputs'].get('max_matches', BUDGETS['max_records']) or self.selected_count > BUDGETS['max_records']:",
            "            raise RuntimeError('[BUDGET_RECORDS_EXCEEDED] selected records exceed budget')",
            f"        extracted = reference_runtime.extract_fields(self.runtime_context, records, fields={extract['inputs']['fields']!r}, operation_id={extract['operation_id']!r})",
            "        for selected, record in zip(records, extracted, strict=True):",
            "            yield {'_sort_key': [document['page_index'], selected['record_index']], 'record': record}",
            "",
            "    def _failed(self, failure):",
            "        self.failure = failure.getErrorMessage()",
            "        if self.crawler.engine is not None:",
            "            self.crawler.engine.close_spider(self, reason='fatal')",
            "",
            "    def _spider_error(self, failure, response, spider):",
            "        self.failure = failure.getErrorMessage()",
            "",
            "    def _item_error(self, item, response, spider, failure):",
            "        self.failure = failure.getErrorMessage()",
            "",
            "    def _spider_closed(self, spider, reason):",
            "        if reason == 'fatal' and self.failure is None:",
            "            self.failure = 'fatal request or callback failure'",
            "        if reason in {'shutdown', 'cancelled'} and self.failure is None:",
            "            self.failure = f'EXECUTION_CANCELLED: Scrapy closed with reason {reason}'",
            "        cardinality = SELECT_OPERATION['inputs'].get('cardinality', 'many')",
            "        if cardinality == 'one' and self.selected_count != 1:",
            "            self.failure = f'CARDINALITY_MISMATCH: expected one record, got {self.selected_count}'",
            "        if cardinality == 'optional_one' and self.selected_count > 1:",
            "            self.failure = f'CARDINALITY_MISMATCH: expected at most one record, got {self.selected_count}'",
            "",
            "def main(argv=None):",
            "    parser = argparse.ArgumentParser()",
            "    parser.add_argument('--fixtures', required=True)",
            "    parser.add_argument('--output', required=True)",
            "    args = parser.parse_args(argv)",
            "    output = Path(args.output).absolute()",
            "    if output.parent.is_symlink() or not output.parent.is_dir() or output.exists():",
            "        raise RuntimeError('[OUTPUT_PATH_UNSAFE] output parent/destination is unsafe')",
            "    settings = Settings(values=scrapy_fixture_runtime.handler_settings(), priority='cmdline')",
            "    process = CrawlerProcess(settings=settings)",
            "    crawler = process.create_crawler(GeneratedSpider)",
            "    process.crawl(crawler, fixture_root=args.fixtures, output_root=args.output)",
            "    process.start(stop_after_crawl=True)",
            "    spider = crawler.spider",
            "    if spider is None or spider.failure is not None:",
            "        if spider is not None:",
            "            shutil.rmtree(spider.context_root, ignore_errors=True)",
            "        print(json.dumps({'state': 'failed', 'diagnostic': None if spider is None else spider.failure}, sort_keys=True))",
            "        return 1",
            "    try:",
            "        report = scrapy_fixture_runtime.publish_output(spider, output, engine={'id': 'scrapy', 'version': __import__('scrapy').__version__, 'backend': 'scrapy-fixture'})",
            "    except Exception as error:",
            "        print(str(error))",
            "        return 1",
            "    print(json.dumps(report, sort_keys=True))",
            "    return 0",
            "",
            "if __name__ == '__main__':",
            "    raise SystemExit(main())",
            "",
        ]
    )
    return "\n".join(lines).encode("utf-8")


def compile_build(
    ir: TypedIR,
    *,
    fixture_root: Path,
    output: Path,
    lockfile: Path,
) -> dict[str, Any]:
    if ir.document.get("ir_version") != IR_VERSION:
        raise PlaybookError(
            "[IR_VERSION_UNSUPPORTED] Scrapy backend requires collector-ir/v1"
        )
    capability_failures = check_capabilities(ir)
    if capability_failures:
        summary = "; ".join(
            f"{item['code']} {item['capability']}" for item in capability_failures
        )
        raise PlaybookError(summary)
    _supported_graph(ir)
    page_map, fixture_diagnostics = inspect_fixture_evidence(fixture_root)
    if fixture_diagnostics:
        raise PlaybookError(
            "; ".join(
                f"{item.code} {item.path}: {item.message}"
                for item in fixture_diagnostics
            )
        )
    evidence_refs = {item["evidence_ref"] for item in ir.document["evidence"]}
    fixtures = [
        {
            "url": page["url"],
            "evidence_ref": page["evidence_ref"],
            "sha256": page["sha256"],
            "size_bytes": page["size_bytes"],
            "content_type": page["content_type"],
            "status": page["status"],
            "discovered_from": page.get("discovered_from"),
            "source_id": page["source_id"],
            "source_revision": page["source_revision"],
        }
        for page in page_map.values()
        if page.get("evidence_ref") in evidence_refs
    ]
    fixtures.sort(key=lambda item: (item["url"], item["evidence_ref"]))
    if not fixtures:
        raise PlaybookError(
            "[FIXTURE_EVIDENCE_MISSING] no build fixtures match the IR evidence"
        )
    if any(
        item["source_id"] != ir.document["source"]["source_id"]
        or item["source_revision"] != ir.document["source"]["revision"]
        for item in fixtures
    ):
        raise PlaybookError("[SOURCE_REVISION_MISMATCH] fixture source differs from IR")
    runtime_files, runtime_identity = runtime_bundle_files(
        ("reference_runtime.py", "syntax.py", "scrapy_fixture_runtime.py")
    )
    files_map = {
        "bot.py": generate_spider(ir, fixtures, runtime_identity),
        "capability-profile.json": canonical_json(PROFILE),
        "README.md": (
            b"# Generated Scrapy fixture bot\n\n"
            b"This Scrapy Spider uses an exact-identity fixture-only HTTP/HTTPS download handler. "
            b"It has no network fallback. The engine runtime is pinned by the BotBuild manifest.\n\n"
            b"Run with the locked mozaic-collector-engineering environment:\n\n"
            b"```sh\npython -B bot.py --fixtures PATH --output PATH\n```\n"
        ),
    }
    files_map.update(runtime_files)
    generated = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in sorted(files_map.items())
    }
    manifest_core = {
        "manifest_version": "bot-build-manifest/v1",
        "playbook_version": "capture-playbook-draft/v2",
        "playbook_sha256": None,
        "ir_version": IR_VERSION,
        "ir_sha256": ir.sha256,
        "backend_id": BACKEND_ID,
        "backend_version": BACKEND_VERSION,
        "capability_profile_sha256": hashlib.sha256(
            canonical_json(PROFILE)
        ).hexdigest(),
        "compiler_version": COMPILER_VERSION,
        "runtime_identity": runtime_identity,
        "toolchain": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform_system": platform.system(),
            "platform_machine": platform.machine(),
            "runtime": SCRAPY_RUNTIME_VERSION,
            "reference_runtime": RUNTIME_VERSION,
            "scrapy": importlib.metadata.version("scrapy"),
            "selectolax": "0.4.13",
        },
        "dependency_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        "generated_files": generated,
        "input_fixtures": fixtures,
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
            if target.is_symlink() or ".." in Path(name).parts:
                raise PlaybookError("[BUILD_PATH_UNSAFE] generated file path is unsafe")
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
    try:
        scrapy_version = importlib.metadata.version("scrapy")
    except importlib.metadata.PackageNotFoundError as error:
        raise PlaybookError(
            "[BUILD_TOOLCHAIN_UNSUPPORTED] Scrapy is not installed"
        ) from error
    return _verify_build(
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
            "runtime": SCRAPY_RUNTIME_VERSION,
            "reference_runtime": RUNTIME_VERSION,
            "scrapy": scrapy_version,
            "selectolax": "0.4.13",
        },
    )
