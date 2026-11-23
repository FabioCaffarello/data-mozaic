"""Offline-only execution helpers for generated Python HTTP Reference bots."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, SelectolaxError

from .syntax import strict_json_loads

RUNTIME_VERSION = "python-http-reference-runtime/1.0.0"
_INTEGER = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)$")
_DECIMAL = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class RuntimeFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code


@dataclass
class RuntimeContext:
    fixture_root: Path
    output_root: Path
    source: dict[str, Any]
    budgets: dict[str, int]
    expected_fixtures: dict[str, dict[str, Any]]
    pages_used: int = 0
    requests_used: int = 0
    bytes_used: int = 0
    records_used: int = 0
    output_bytes_used: int = 0

    @classmethod
    def create(
        cls,
        fixture_root: str,
        output_root: str,
        source: dict[str, Any],
        budgets: dict[str, int],
        expected_fixtures: list[dict[str, Any]] | None = None,
    ):
        fixtures = _safe_directory(Path(fixture_root), must_exist=True)
        output = _safe_directory(Path(output_root), must_exist=False)
        expected_by_url = {
            item["url"]: dict(item) for item in (expected_fixtures or [])
        }
        return cls(fixtures, output, source, budgets, expected_by_url)


def _safe_directory(path: Path, *, must_exist: bool) -> Path:
    if ".." in path.parts:
        raise RuntimeFailure("OUTPUT_PATH_UNSAFE", "path traversal is not allowed")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise RuntimeFailure(
                "OUTPUT_PATH_UNSAFE", f"symlink path component: {current}"
            )
    if must_exist:
        if not path.is_dir() or path.is_symlink():
            raise RuntimeFailure(
                "FIXTURE_ROOT_INVALID", "fixture root must be a real directory"
            )
        return path.resolve()
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise RuntimeFailure(
                "OUTPUT_PATH_UNSAFE", "output root must be a real directory"
            )
        if any(path.iterdir()):
            raise RuntimeFailure("OUTPUT_NOT_EMPTY", "output root must be empty")
        return path.resolve()
    path.mkdir(parents=True, mode=0o700)
    return path.resolve()


def _scoped(url: str, source: dict[str, Any]) -> bool:
    try:
        candidate, origin = urlsplit(url), urlsplit(source["origin"])
        candidate_port, origin_port = candidate.port, origin.port
    except ValueError:
        return False
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin_port not in (None, 443)
        or origin.path not in ("", "/")
        or origin.query
        or origin.fragment
    ):
        return False
    if (candidate.scheme, candidate.hostname, candidate_port) != (
        origin.scheme,
        origin.hostname,
        origin_port,
    ):
        return False
    if (
        candidate.username is not None
        or candidate.password is not None
        or candidate.fragment
        or any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url)
        or any(char in url for char in "\\%")
        or "//" in candidate.path
        or any(segment in {".", ".."} for segment in candidate.path.split("/"))
    ):
        return False
    path = candidate.path or "/"
    for prefix in source["path_prefixes"]:
        if (
            not prefix.startswith("/")
            or "\\" in prefix
            or "%" in prefix
            or "//" in prefix
            or any(segment in {".", ".."} for segment in prefix.split("/"))
        ):
            return False
    return any(
        prefix == "/"
        or path == prefix.rstrip("/")
        or path.startswith(prefix.rstrip("/") + "/")
        for prefix in source["path_prefixes"]
    )


def _fixture_index(ctx: RuntimeContext) -> dict[str, Any]:
    path = ctx.fixture_root / "index.json"
    if path.is_symlink() or not path.is_file():
        raise RuntimeFailure("FIXTURE_INDEX_MISSING", "fixture index is missing")
    try:
        if path.stat().st_size > 1_000_000:
            raise RuntimeFailure(
                "FIXTURE_INDEX_TOO_LARGE", "fixture index exceeds 1 MB"
            )
        index = strict_json_loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeFailure("FIXTURE_INDEX_INVALID", str(error)) from error
    if (
        not isinstance(index, dict)
        or not isinstance(index.get("pages"), list)
        or len(index["pages"]) > 200
    ):
        raise RuntimeFailure(
            "FIXTURE_INDEX_INVALID", "fixture index must contain at most 200 pages"
        )
    fixture_source = index.get("source")
    if (
        not isinstance(fixture_source, dict)
        or fixture_source.get("source_id") != ctx.source["source_id"]
        or fixture_source.get("revision") != ctx.source["revision"]
    ):
        raise RuntimeFailure(
            "SOURCE_REVISION_MISMATCH",
            "fixture index source/revision differs from the compiled source",
        )
    seen_urls: set[str] = set()
    total_size = 0
    for number, page in enumerate(index["pages"]):
        if not isinstance(page, dict):
            raise RuntimeFailure(
                "FIXTURE_INDEX_INVALID", f"fixture page {number} must be an object"
            )
        page_url = page.get("url")
        if not isinstance(page_url, str) or not page_url or page_url in seen_urls:
            raise RuntimeFailure(
                "FIXTURE_INDEX_INVALID",
                f"fixture page {number} has a missing or duplicate URL",
            )
        seen_urls.add(page_url)
        body_name = page.get("body_file")
        if not isinstance(body_name, str) or not body_name:
            raise RuntimeFailure(
                "FIXTURE_PATH_UNSAFE", f"fixture page {number} has no safe body path"
            )
        relative = Path(body_name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or "\\" in body_name
            or any(part in {"", "."} for part in relative.parts)
        ):
            raise RuntimeFailure(
                "FIXTURE_PATH_UNSAFE", f"fixture page {number} has an unsafe body path"
            )
        body_path = ctx.fixture_root / relative
        current = ctx.fixture_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise RuntimeFailure(
                    "FIXTURE_PATH_UNSAFE", "fixture body cannot be a symlink"
                )
        if not body_path.is_file() or body_path.stat().st_size > 2_000_000:
            raise RuntimeFailure(
                "FIXTURE_TOO_LARGE", "fixture body must be a regular file at most 2 MB"
            )
        total_size += body_path.stat().st_size
        if total_size > 24_000_000:
            raise RuntimeFailure(
                "FIXTURE_BUDGET_EXCEEDED", "fixture bodies exceed 24 MB"
            )
        size_bytes = page.get("size_bytes")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes != body_path.stat().st_size
            or not isinstance(page.get("evidence_ref"), str)
            or not page.get("evidence_ref")
            or not isinstance(page.get("sha256"), str)
            or not isinstance(page.get("content_type"), str)
            or not isinstance(page.get("status"), int)
            or isinstance(page.get("status"), bool)
        ):
            raise RuntimeFailure(
                "FIXTURE_METADATA_INVALID",
                f"fixture page {number} metadata is incomplete or inconsistent",
            )
    for expected_url, expected in ctx.expected_fixtures.items():
        actual = next(
            (page for page in index["pages"] if page.get("url") == expected_url), None
        )
        if (
            actual is None
            or any(
                actual.get(key) != expected.get(key)
                for key in (
                    "evidence_ref",
                    "sha256",
                    "size_bytes",
                    "content_type",
                    "status",
                )
            )
            or (
                expected.get("source_id") != fixture_source.get("source_id")
                or expected.get("source_revision") != fixture_source.get("revision")
            )
        ):
            raise RuntimeFailure(
                "FIXTURE_IDENTITY_MISMATCH",
                f"fixture identity differs from compiled build: {expected_url}",
            )
    if index.get("complete") is not True:
        raise RuntimeFailure(
            "FIXTURE_INDEX_INCOMPLETE", "fixture index must declare complete=true"
        )
    return index


def fetch_fixture(
    ctx: RuntimeContext, *, url: str, content_type: str, evidence_refs: list[str]
) -> dict[str, Any]:
    if not _scoped(url, ctx.source):
        raise RuntimeFailure("URL_OUT_OF_SCOPE", f"URL is outside source scope: {url}")
    if ctx.requests_used + 1 > ctx.budgets["max_requests"]:
        raise RuntimeFailure("BUDGET_REQUESTS_EXCEEDED", "request budget exhausted")
    if ctx.pages_used + 1 > ctx.budgets["max_pages"]:
        raise RuntimeFailure("BUDGET_PAGES_EXCEEDED", "page budget exhausted")
    page = next(
        (item for item in _fixture_index(ctx)["pages"] if item.get("url") == url), None
    )
    if page is None:
        raise RuntimeFailure("FIXTURE_NOT_FOUND", f"no offline fixture for {url}")
    expected = ctx.expected_fixtures.get(url)
    if ctx.expected_fixtures and expected is None:
        raise RuntimeFailure(
            "FIXTURE_NOT_IN_BUILD",
            f"URL was not included in compiled fixture identities: {url}",
        )
    body_name = page["body_file"]
    body_relative = Path(body_name)
    body_path = ctx.fixture_root / body_relative
    current = ctx.fixture_root
    unsafe_component = False
    for part in body_relative.parts:
        current = current / part
        unsafe_component = unsafe_component or current.is_symlink()
    if (
        body_relative.is_absolute()
        or ".." in body_relative.parts
        or "\\" in body_name
        or unsafe_component
    ):
        raise RuntimeFailure(
            "FIXTURE_PATH_UNSAFE", "fixture path must remain below fixture root"
        )
    try:
        body = body_path.read_bytes()
    except OSError as error:
        raise RuntimeFailure("FIXTURE_BODY_MISSING", str(error)) from error
    digest = hashlib.sha256(body).hexdigest()
    if digest != page.get("sha256"):
        raise RuntimeFailure("FIXTURE_DIGEST_MISMATCH", f"digest mismatch for {url}")
    if expected is not None and digest != expected.get("sha256"):
        raise RuntimeFailure(
            "FIXTURE_IDENTITY_MISMATCH",
            f"fixture digest differs from compiled build: {url}",
        )
    if page.get("evidence_ref") not in evidence_refs:
        raise RuntimeFailure(
            "EVIDENCE_NOT_REFERENCED", f"fixture evidence not bound to fetch: {url}"
        )
    if page.get("status") != 200:
        raise RuntimeFailure(
            "HTTP_STATUS_INVALID", f"fixture status is {page.get('status')}"
        )
    if page.get("content_type") != content_type:
        raise RuntimeFailure(
            "CONTENT_TYPE_MISMATCH",
            f"expected {content_type}, got {page.get('content_type')}",
        )
    if (
        len(body) > ctx.budgets["max_response_bytes"]
        or ctx.bytes_used + len(body) > ctx.budgets["max_response_bytes"]
    ):
        raise RuntimeFailure("BUDGET_BYTES_EXCEEDED", "response byte budget exhausted")
    ctx.requests_used += 1
    ctx.pages_used += 1
    ctx.bytes_used += len(body)
    return {
        "url": url,
        "body": body,
        "sha256": digest,
        "evidence_ref": page["evidence_ref"],
        "content_type": content_type,
        "status": 200,
    }


def discover_links(
    ctx: RuntimeContext,
    document: dict[str, Any],
    *,
    selector: str,
    max_links: int,
    evidence_refs: list[str],
) -> dict[str, Any]:
    if document["evidence_ref"] not in evidence_refs:
        raise RuntimeFailure(
            "EVIDENCE_NOT_REFERENCED", "link discovery document has no evidence binding"
        )
    parser = _html(document["body"])
    try:
        candidates = parser.css(selector)
    except SelectolaxError as error:
        raise RuntimeFailure("SELECTOR_INVALID", str(error)) from error
    links = []
    for node in candidates:
        href = node.attributes.get("href")
        if not href:
            continue
        url = urljoin(document["url"], href)
        if not _scoped(url, ctx.source):
            raise RuntimeFailure(
                "URL_OUT_OF_SCOPE", f"discovered link is outside source scope: {url}"
            )
        if url not in links:
            links.append(url)
        if len(links) > max_links:
            raise RuntimeFailure(
                "BUDGET_LINKS_EXCEEDED", "discovered links exceed max_links"
            )
    return {"seed": document, "urls": links, "evidence_refs": evidence_refs}


def paginate(
    ctx: RuntimeContext, link_set: dict[str, Any], *, max_pages: int
) -> list[dict[str, Any]]:
    if max_pages > ctx.budgets["max_pages"]:
        raise RuntimeFailure(
            "BUDGET_PAGES_EXCEEDED", "pagination exceeds the page budget"
        )
    documents = [link_set["seed"]]
    visited = {link_set["seed"]["url"]}
    for url in link_set["urls"]:
        if len(documents) >= max_pages:
            break
        if url in visited:
            continue
        visited.add(url)
        seed_ref = link_set["seed"]["evidence_ref"]
        index = _fixture_index(ctx)
        page = next((item for item in index["pages"] if item.get("url") == url), None)
        if page is None:
            raise RuntimeFailure(
                "FIXTURE_NOT_FOUND", f"no offline fixture for discovered link {url}"
            )
        document = fetch_fixture(
            ctx,
            url=url,
            content_type=page["content_type"],
            evidence_refs=[page["evidence_ref"]],
        )
        if page.get("discovered_from") != seed_ref:
            raise RuntimeFailure(
                "LINK_NOT_OBSERVED",
                f"fixture index does not prove link from evidence {seed_ref}",
            )
        documents.append(document)
    return documents


def _html(body: bytes) -> LexborHTMLParser:
    try:
        return LexborHTMLParser(body.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise RuntimeFailure(
            "HTML_ENCODING_UNSUPPORTED", "HTML fixture must be UTF-8"
        ) from error


def select_html_records(
    ctx: RuntimeContext,
    documents: dict[str, Any] | list[dict[str, Any]],
    *,
    selector: str,
    cardinality: str,
    max_matches: int,
    evidence_refs: list[str],
) -> list[dict[str, Any]]:
    pages = documents if isinstance(documents, list) else [documents]
    selected = []
    for page in pages:
        if page["evidence_ref"] not in evidence_refs:
            raise RuntimeFailure(
                "EVIDENCE_NOT_REFERENCED",
                f"selector evidence missing for {page['url']}",
            )
        parser = _html(page["body"])
        try:
            matches = parser.css(selector)
        except SelectolaxError as error:
            raise RuntimeFailure("SELECTOR_INVALID", str(error)) from error
        for node in matches:
            selected.append({"kind": "html", "node": node, "page": page})
            if (
                len(selected) > max_matches
                or len(selected) > ctx.budgets["max_records"]
            ):
                raise RuntimeFailure(
                    "BUDGET_RECORDS_EXCEEDED",
                    "selected records exceed the record budget",
                )
    if cardinality == "one" and len(selected) != 1:
        raise RuntimeFailure(
            "CARDINALITY_MISMATCH", f"expected exactly one match, got {len(selected)}"
        )
    if cardinality == "optional_one" and len(selected) > 1:
        raise RuntimeFailure(
            "CARDINALITY_MISMATCH", f"expected zero or one match, got {len(selected)}"
        )
    return selected


def _json_path(value: Any, path: str, *, expand: bool) -> list[tuple[Any, str]]:
    if not path.startswith("/"):
        raise RuntimeFailure("JSON_PATH_INVALID", "path must start with '/'")
    raw = path.split("/")[1:]
    if expand and (raw.count("*") != 1 or raw[-1] != "*"):
        raise RuntimeFailure(
            "JSON_PATH_INVALID", "array path supports one final /* expansion"
        )
    if not expand and "*" in raw:
        raise RuntimeFailure(
            "JSON_PATH_INVALID", "field path does not support wildcards"
        )
    current = [(value, "")]
    for segment in raw:
        next_values = []
        for item, pointer in current:
            if segment == "*":
                if not isinstance(item, list):
                    raise RuntimeFailure(
                        "JSON_PATH_TYPE_MISMATCH", "wildcard target is not an array"
                    )
                next_values.extend(
                    (entry, f"{pointer}/{index}") for index, entry in enumerate(item)
                )
                continue
            key = segment.replace("~1", "/").replace("~0", "~")
            if "~" in key and re.search(r"~(?![01])", segment):
                raise RuntimeFailure("JSON_PATH_INVALID", "invalid JSON pointer escape")
            if isinstance(item, list) and not re.fullmatch(r"0|[1-9][0-9]*", key):
                raise RuntimeFailure(
                    "JSON_PATH_INVALID",
                    "array indices must be canonical non-negative integers",
                )
            try:
                next_item = item[int(key)] if isinstance(item, list) else item[key]
            except (KeyError, IndexError, ValueError, TypeError) as error:
                raise RuntimeFailure(
                    "JSON_PATH_MISSING", f"path segment {key!r} is missing"
                ) from error
            next_values.append((next_item, f"{pointer}/{segment}"))
        current = next_values
    return current


def select_json_records(
    ctx: RuntimeContext,
    document: dict[str, Any],
    *,
    array_path: str,
    max_matches: int,
    evidence_refs: list[str],
) -> list[dict[str, Any]]:
    if document["evidence_ref"] not in evidence_refs:
        raise RuntimeFailure(
            "EVIDENCE_NOT_REFERENCED", "JSON selection has no evidence binding"
        )
    try:
        root = strict_json_loads(document["body"], parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeFailure("JSON_DOCUMENT_INVALID", str(error)) from error
    values = _json_path(root, array_path, expand=True)
    if len(values) > max_matches or len(values) > ctx.budgets["max_records"]:
        raise RuntimeFailure(
            "BUDGET_RECORDS_EXCEEDED", "JSON selection exceeds record budget"
        )
    return [
        {"kind": "json", "value": item, "page": document, "pointer": pointer}
        for item, pointer in values
    ]


def _raw_value(record: dict[str, Any], field: dict[str, Any]) -> Any:
    if record["kind"] == "html":
        node = record["node"]
        if field.get("selector"):
            try:
                node = node.css_first(field["selector"])
            except SelectolaxError as error:
                raise RuntimeFailure("SELECTOR_INVALID", str(error)) from error
        if node is None:
            return None
        if field["source"] == "text":
            return node.text(deep=True, separator=" ")
        return node.attributes.get(field["attribute"])
    if field["source"] != "json_value":
        raise RuntimeFailure(
            "FIELD_MAPPING_INVALID", "JSON record requires json_value field source"
        )
    try:
        values = _json_path(record["value"], field["path"], expand=False)
    except RuntimeFailure as error:
        if error.code == "JSON_PATH_MISSING":
            return None
        raise
    return values[0][0] if values else None


def _convert(value: Any, field: dict[str, Any], name: str) -> Any:
    if value is None:
        if field["required"]:
            raise RuntimeFailure(
                "REQUIRED_FIELD_MISSING", f"required field {name!r} is absent"
            )
        return None
    if isinstance(value, (dict, list)):
        raise RuntimeFailure(
            "FIELD_CONVERSION_INVALID", f"field {name!r} must be a scalar"
        )
    if isinstance(value, str) and len(value.encode("utf-8")) > 16_384:
        raise RuntimeFailure("OUTPUT_VALUE_TOO_LARGE", f"field {name!r} exceeds 16 KiB")
    kind = field["type"]
    if kind == "boolean" and isinstance(value, bool):
        return value
    if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
        return value
    if kind == "decimal" and isinstance(value, Decimal):
        decimal_tuple = value.as_tuple()
        if len(decimal_tuple.digits) > 64 or abs(decimal_tuple.exponent) > 64:
            raise RuntimeFailure(
                "FIELD_CONVERSION_INVALID", f"field {name!r} decimal is too large"
            )
        canonical = format(value, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        return "0" if canonical in {"-0", "+0", ""} else canonical
    if kind == "decimal" and isinstance(value, int) and not isinstance(value, bool):
        if len(str(abs(value))) > 128:
            raise RuntimeFailure(
                "FIELD_CONVERSION_INVALID", f"field {name!r} integer is too large"
            )
        return str(value)
    if kind == "string" and not isinstance(value, str):
        raise RuntimeFailure(
            "FIELD_CONVERSION_INVALID", f"field {name!r} is not a string"
        )
    text = value if isinstance(value, str) else str(value)
    for rule in field["normalize"]:
        if rule == "trim":
            text = text.strip()
        elif rule == "collapse_whitespace":
            text = " ".join(text.split())
    if kind == "string":
        return text
    if kind == "integer" and _INTEGER.fullmatch(text):
        if len(text.lstrip("+-")) > 128:
            raise RuntimeFailure(
                "FIELD_CONVERSION_INVALID", f"field {name!r} integer is too large"
            )
        return int(text)
    if kind == "decimal" and _DECIMAL.fullmatch(text):
        try:
            value_decimal = Decimal(text)
        except InvalidOperation as error:
            raise RuntimeFailure(
                "FIELD_CONVERSION_INVALID", f"field {name!r} is not a decimal"
            ) from error
        decimal_tuple = value_decimal.as_tuple()
        if len(decimal_tuple.digits) > 64 or abs(decimal_tuple.exponent) > 64:
            raise RuntimeFailure(
                "FIELD_CONVERSION_INVALID", f"field {name!r} decimal is too large"
            )
        if not value_decimal.is_finite():
            raise RuntimeFailure(
                "FIELD_CONVERSION_INVALID", f"field {name!r} is not finite"
            )
        canonical = format(value_decimal, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        return "0" if canonical in {"-0", "+0", ""} else canonical
    if kind == "boolean" and text in {"true", "false"}:
        return text == "true"
    raise RuntimeFailure(
        "FIELD_CONVERSION_INVALID",
        f"field {name!r} cannot be converted to {kind}: {text!r}",
    )


def extract_fields(
    ctx: RuntimeContext,
    records: list[dict[str, Any]],
    *,
    fields: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    extracted = []
    for record in records:
        data = {
            name: _convert(_raw_value(record, field), field, name)
            for name, field in fields.items()
        }
        page = record["page"]
        data["_provenance"] = {
            "source_url": page["url"],
            "evidence_ref": page["evidence_ref"],
            "raw_sha256": page["sha256"],
        }
        _reserve_output(ctx, len(canonical_record(data)) + 1)
        extracted.append(data)
        ctx.records_used += 1
        if ctx.records_used > ctx.budgets["max_records"]:
            raise RuntimeFailure(
                "BUDGET_RECORDS_EXCEEDED", "extracted records exceed budget"
            )
    return extracted


def _reserve_output(ctx: RuntimeContext, size: int) -> None:
    if ctx.output_bytes_used + size > ctx.budgets["max_output_bytes"]:
        raise RuntimeFailure(
            "BUDGET_OUTPUT_BYTES_EXCEEDED", "output byte budget exhausted"
        )
    ctx.output_bytes_used += size


def write_records(ctx: RuntimeContext, records: list[dict[str, Any]]) -> dict[str, Any]:
    path = ctx.output_root / "records.jsonl"
    data = b"".join(canonical_record(record) + b"\n" for record in records)
    _write_new(path, data)
    report = {
        "state": "passed",
        "runtime": RUNTIME_VERSION,
        "pages": ctx.pages_used,
        "requests": ctx.requests_used,
        "response_bytes": ctx.bytes_used,
        "records": len(records),
        "records_file": "records.jsonl",
        "records_sha256": hashlib.sha256(data).hexdigest(),
    }
    return report


def write_report(ctx: RuntimeContext, report: dict[str, Any]) -> None:
    data = json.dumps(report, sort_keys=True, indent=2).encode() + b"\n"
    _reserve_output(ctx, len(data))
    _write_new(ctx.output_root / "report.json", data)


def write_raw_references(
    ctx: RuntimeContext, documents: dict[str, Any] | list[dict[str, Any]]
) -> list[dict[str, str]]:
    pages = documents if isinstance(documents, list) else [documents]
    refs = [
        {
            "url": page["url"],
            "evidence_ref": page["evidence_ref"],
            "sha256": page["sha256"],
        }
        for page in pages
    ]
    data = json.dumps(refs, sort_keys=True, indent=2).encode() + b"\n"
    _reserve_output(ctx, len(data))
    _write_new(ctx.output_root / "raw-references.json", data)
    return refs


def canonical_record(record: dict[str, Any]) -> bytes:
    return json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _write_new(path: Path, data: bytes) -> None:
    if path.is_symlink() or path.exists():
        raise RuntimeFailure("OUTPUT_PATH_UNSAFE", f"refusing to overwrite {path.name}")
    if path.parent.resolve() != path.parent:
        raise RuntimeFailure("OUTPUT_PATH_UNSAFE", "output root changed during run")
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError as error:
        raise RuntimeFailure(
            "OUTPUT_PATH_UNSAFE", f"refusing to overwrite {path.name}"
        ) from error
