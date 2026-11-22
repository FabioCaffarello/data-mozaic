"""Closed draft/v2 CapturePlaybook parser, semantic validator, and Typed IR."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from selectolax.lexbor import SelectolaxError

from .syntax import PlaybookError, UniqueKeyLoader

SCHEMA = json.loads(
    files("collector_engineering")
    .joinpath("schemas/CapturePlaybook.v2.schema.json")
    .read_text(encoding="utf-8")
)
DSL_VERSION = "capture-playbook-draft/v2"
IR_VERSION = "collector-ir/v1"
OPERATOR_VERSIONS = {
    "http.fetch/v1",
    "links.discover/v1",
    "pagination.bounded/v1",
    "browser.render_dom/v1",
    "select.html_records/v1",
    "select.json_records/v1",
    "select.rendered_dom_records/v1",
    "extract.fields/v1",
    "output.records/v1",
    "output.raw/v1",
}
CAPABILITY_BY_OP = {
    "http.fetch/v1": "http.fetch.fixture",
    "links.discover/v1": "links.discover.fixture",
    "pagination.bounded/v1": "pagination.bounded.fixture",
    "browser.render_dom/v1": "browser.rendered_dom",
    "select.html_records/v1": "html.css_select",
    "select.json_records/v1": "json.path_restricted",
    "select.rendered_dom_records/v1": "browser.rendered_dom",
    "extract.fields/v1": "records.extract",
    "output.records/v1": "output.records.jsonl",
    "output.raw/v1": "output.raw.reference",
}
_CAPABILITY_ORDER = tuple(CAPABILITY_BY_OP.values())


@dataclass(frozen=True)
class Diagnostic:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    diagnostics: tuple[Diagnostic, ...]
    evidence_sufficient: bool


@dataclass(frozen=True)
class ParsedPlaybook:
    document: dict[str, Any]
    source_path: str | None = None


@dataclass(frozen=True)
class TypedIR:
    document: dict[str, Any]

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.document)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def canonical_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def parse_playbook(text: str, *, source_path: str | None = None) -> ParsedPlaybook:
    if len(text.encode("utf-8")) > 1_000_000:
        raise PlaybookError("[PLAYBOOK_TOO_LARGE] playbook exceeds the 1 MB limit")
    try:
        if any(
            isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken))
            for token in yaml.scan(text)
        ):
            raise PlaybookError("YAML anchors and aliases are not supported")
        value = yaml.load(text, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, UnicodeError, PlaybookError) as error:
        raise PlaybookError(f"[PLAYBOOK_PARSE_ERROR] {error}") from error
    if not isinstance(value, dict):
        raise PlaybookError("[PLAYBOOK_ROOT_INVALID] playbook root must be a mapping")
    return ParsedPlaybook(value, source_path)


def load_playbook_v2(path: Path) -> ParsedPlaybook:
    return parse_playbook(path.read_text(encoding="utf-8"), source_path=str(path))


def _schema_diagnostics(document: dict[str, Any]) -> list[Diagnostic]:
    if document.get("dsl_version") != DSL_VERSION:
        return [
            Diagnostic(
                "DSL_VERSION_UNSUPPORTED",
                "$.dsl_version",
                "expected capture-playbook-draft/v2; v1 must use the v1 validator",
            )
        ]
    result = []
    validator = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
    for error in validator.iter_errors(document):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        if (
            path.endswith(".op")
            and isinstance(error.instance, str)
            and error.instance not in OPERATOR_VERSIONS
        ):
            result.append(
                Diagnostic(
                    "UNKNOWN_OPERATION", path, f"unknown operation {error.instance!r}"
                )
            )
        else:
            result.append(Diagnostic("SCHEMA_INVALID", path, error.message))
    # oneOf obscures an unsupported operation in its parent diagnostic.
    for index, operation in enumerate(document.get("operations", [])):
        if isinstance(operation, dict) and operation.get("op") not in OPERATOR_VERSIONS:
            path = f"$.operations[{index}].op"
            if not any(
                item.code == "UNKNOWN_OPERATION" and item.path == path
                for item in result
            ):
                result.append(
                    Diagnostic(
                        "UNKNOWN_OPERATION",
                        path,
                        f"unknown operation {operation.get('op')!r}",
                    )
                )
    ids = [
        item.get("id")
        for item in document.get("operations", [])
        if isinstance(item, dict)
    ]
    if len(ids) != len(set(ids)):
        result.append(
            Diagnostic(
                "OPERATION_ID_DUPLICATE", "$.operations", "operation IDs must be unique"
            )
        )
    finding_ids = [
        item.get("finding_id")
        for item in document.get("findings", [])
        if isinstance(item, dict)
    ]
    if len(finding_ids) != len(set(finding_ids)):
        result.append(
            Diagnostic(
                "FINDING_ID_DUPLICATE", "$.findings", "finding IDs must be unique"
            )
        )
    evidence_ids = [
        item.get("evidence_ref")
        for item in document.get("evidence", [])
        if isinstance(item, dict)
    ]
    if len(evidence_ids) != len(set(evidence_ids)):
        result.append(
            Diagnostic(
                "EVIDENCE_ID_DUPLICATE", "$.evidence", "evidence refs must be unique"
            )
        )
    return result


def _within_scope(url: str, source: dict[str, Any]) -> bool:
    try:
        candidate = urlsplit(url)
        origin = urlsplit(source["origin"])
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or origin.port not in (None, 443)
            or origin.path not in ("", "/")
            or origin.query
            or origin.fragment
        ):
            return False
        if (candidate.scheme, candidate.hostname, candidate.port) != (
            origin.scheme,
            origin.hostname,
            origin.port,
        ):
            return False
        if (
            candidate.username is not None
            or candidate.password is not None
            or candidate.fragment
            or any(
                char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in url
            )
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
            path == prefix.rstrip("/")
            or path.startswith(prefix.rstrip("/") + "/")
            or prefix == "/"
            for prefix in source["path_prefixes"]
        )
    except (KeyError, TypeError, ValueError):
        return False


def _fixture_evidence(
    fixture_root: Path | None,
) -> tuple[dict[str, dict[str, Any]], list[Diagnostic]]:
    if fixture_root is None:
        return {}, []
    index_path = fixture_root / "index.json"
    if not index_path.is_file() or index_path.is_symlink():
        return {}, [
            Diagnostic(
                "FIXTURE_INDEX_MISSING",
                "$.evidence",
                "fixture index.json is missing or unsafe",
            )
        ]
    if len(fixture_root.parts) and any(
        part.is_symlink() for part in (fixture_root, *fixture_root.parents)
    ):
        return {}, [
            Diagnostic(
                "FIXTURE_ROOT_INVALID",
                "$.evidence",
                "fixture root cannot contain symlink path components",
            )
        ]
    try:
        if index_path.stat().st_size > 1_000_000:
            return {}, [
                Diagnostic(
                    "FIXTURE_INDEX_TOO_LARGE",
                    "$.evidence",
                    "fixture index exceeds 1 MB",
                )
            ]
    except OSError as error:
        return {}, [Diagnostic("FIXTURE_INDEX_INVALID", "$.evidence", str(error))]
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {}, [Diagnostic("FIXTURE_INDEX_INVALID", "$.evidence", str(error))]
    if (
        not isinstance(index, dict)
        or not isinstance(index.get("pages"), list)
        or len(index["pages"]) > 200
        or index.get("complete") is not True
    ):
        return {}, [
            Diagnostic(
                "FIXTURE_INDEX_INVALID",
                "$.evidence",
                "fixture index must contain at most 200 pages",
            )
        ]
    page_map: dict[str, dict[str, Any]] = {}
    diagnostics = []
    total_bytes = 0
    for number, page in enumerate(index.get("pages", [])):
        if not isinstance(page, dict):
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_INDEX_INVALID",
                    f"$.fixtures[{number}]",
                    "fixture page entry must be an object",
                )
            )
            continue
        body_name = page.get("body_file", "")
        if not isinstance(body_name, str) or not body_name:
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_PATH_UNSAFE",
                    f"$.fixtures[{number}]",
                    "fixture body path must be a non-empty string",
                )
            )
            continue
        body_relative = Path(body_name)
        body_path = fixture_root / body_relative
        current = fixture_root
        unsafe_component = False
        for part in body_relative.parts:
            current = current / part
            unsafe_component = unsafe_component or current.is_symlink()
        if (
            not body_name
            or body_relative.is_absolute()
            or ".." in body_relative.parts
            or "\\" in body_name
            or unsafe_component
        ):
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_PATH_UNSAFE",
                    f"$.fixtures[{number}]",
                    "fixture body path is unsafe",
                )
            )
            continue
        try:
            if not body_path.is_file():
                raise OSError("fixture body is not a regular file")
            if body_path.stat().st_size > 2_000_000:
                raise OSError("fixture body exceeds 2 MB")
            body = body_path.read_bytes()
        except OSError as error:
            diagnostics.append(
                Diagnostic("FIXTURE_BODY_MISSING", f"$.fixtures[{number}]", str(error))
            )
            continue
        total_bytes += len(body)
        if total_bytes > 24_000_000:
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_BUDGET_EXCEEDED",
                    "$.fixtures",
                    "fixture bodies exceed the 24 MB validation limit",
                )
            )
            break
        declared_size = page.get("size_bytes")
        if declared_size is not None and declared_size != len(body):
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_SIZE_MISMATCH",
                    f"$.fixtures[{number}].size_bytes",
                    "declared fixture size differs from the body",
                )
            )
            continue
        actual = hashlib.sha256(body).hexdigest()
        if actual != page.get("sha256"):
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_DIGEST_MISMATCH",
                    f"$.fixtures[{number}].sha256",
                    f"expected {page.get('sha256')}, got {actual}",
                )
            )
            continue
        url = page.get("url", "")
        if not isinstance(url, str) or not url:
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_INDEX_INVALID",
                    f"$.fixtures[{number}].url",
                    "fixture URL must be a non-empty string",
                )
            )
            continue
        if url in page_map:
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_URL_DUPLICATE",
                    f"$.fixtures[{number}].url",
                    f"fixture URL {url!r} occurs more than once",
                )
            )
            continue
        page_map[url] = {**page, "body": body, "body_path": body_path}
    return page_map, diagnostics


def validate_playbook_v2(
    parsed: ParsedPlaybook | dict[str, Any], *, fixture_root: Path | None = None
) -> ValidationResult:
    document = parsed.document if isinstance(parsed, ParsedPlaybook) else parsed
    diagnostics = _schema_diagnostics(document)
    if diagnostics:
        return ValidationResult(
            False, tuple(sorted(diagnostics, key=lambda d: (d.path, d.code))), False
        )

    operations = document["operations"]
    by_id = {operation["id"]: operation for operation in operations}
    source = document["source"]
    evidence = {item["evidence_ref"]: item["sha256"] for item in document["evidence"]}
    finding_evidence = {
        ref for finding in document["findings"] for ref in finding["evidence_refs"]
    }
    for index, ref in enumerate(evidence):
        if ref not in finding_evidence:
            diagnostics.append(
                Diagnostic(
                    "MISSING_FINDING_EVIDENCE",
                    f"$.evidence[{index}]",
                    f"evidence {ref!r} is not referenced by a finding",
                )
            )
    evidence_sufficient = fixture_root is not None
    for index, finding in enumerate(document["findings"]):
        for ref in finding["evidence_refs"]:
            if ref not in evidence:
                diagnostics.append(
                    Diagnostic(
                        "EVIDENCE_REFERENCE_MISSING",
                        f"$.findings[{index}].evidence_refs",
                        f"unknown evidence ref {ref!r}",
                    )
                )
                evidence_sufficient = False
    fixture_pages, fixture_diagnostics = _fixture_evidence(fixture_root)
    diagnostics.extend(fixture_diagnostics)
    if fixture_root is not None:
        index = {}
        index_path = fixture_root / "index.json"
        if fixture_pages and index_path.is_file() and not index_path.is_symlink():
            try:
                candidate = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(candidate, dict):
                    index = candidate
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
        fixture_source = index.get("source", {})
        if (
            isinstance(fixture_source, dict)
            and fixture_source
            and (
                fixture_source.get("source_id") != source.get("source_id")
                or fixture_source.get("revision") != source.get("revision")
            )
        ):
            diagnostics.append(
                Diagnostic(
                    "SOURCE_REVISION_MISMATCH",
                    "$.source",
                    "playbook source/revision differs from fixture index",
                )
            )
        for ref, digest in evidence.items():
            matches = [
                page
                for page in fixture_pages.values()
                if page.get("evidence_ref") == ref
            ]
            if not matches or all(page.get("sha256") != digest for page in matches):
                diagnostics.append(
                    Diagnostic(
                        "FIXTURE_EVIDENCE_MISMATCH",
                        "$.evidence",
                        f"evidence {ref!r} does not match a verified fixture digest",
                    )
                )
                evidence_sufficient = False

    outputs: dict[str, dict[str, str]] = {}
    dependencies: dict[str, set[str]] = {
        operation["id"]: set() for operation in operations
    }
    inferred_capabilities: set[str] = set()
    extraction_seen = False
    output_seen = False
    for index, operation in enumerate(operations):
        op = operation["op"]
        node_id = operation["id"]
        op_path = f"$.operations[{index}]"
        inferred_capabilities.add(CAPABILITY_BY_OP[op])
        if op == "http.fetch/v1":
            if not _within_scope(operation["url"], source):
                diagnostics.append(
                    Diagnostic(
                        "URL_OUT_OF_SCOPE",
                        f"{op_path}.url",
                        "fetch URL is outside the declared source scope",
                    )
                )
            outputs[node_id] = {
                "document": "HtmlDocument"
                if operation["content_type"] == "text/html"
                else "JsonDocument"
            }
        elif op == "links.discover/v1":
            outputs[node_id] = {"links": "LinkSet"}
        elif op == "pagination.bounded/v1":
            if operation["max_pages"] > document["budgets"]["max_pages"]:
                diagnostics.append(
                    Diagnostic(
                        "BUDGET_OVERFLOW",
                        f"{op_path}.max_pages",
                        "pagination exceeds the playbook page budget",
                    )
                )
            outputs[node_id] = {"documents": "PageSet"}
        elif op == "browser.render_dom/v1":
            outputs[node_id] = {"rendered_document": "RenderedDocument"}
        elif op == "select.html_records/v1":
            outputs[node_id] = {"records": "HtmlRecords"}
            if operation["max_matches"] > document["budgets"]["max_records"]:
                diagnostics.append(
                    Diagnostic(
                        "BUDGET_OVERFLOW",
                        f"{op_path}.max_matches",
                        "selection maximum exceeds record budget",
                    )
                )
        elif op == "select.json_records/v1":
            outputs[node_id] = {"records": "JsonRecords"}
            if operation["max_matches"] > document["budgets"]["max_records"]:
                diagnostics.append(
                    Diagnostic(
                        "BUDGET_OVERFLOW",
                        f"{op_path}.max_matches",
                        "selection maximum exceeds record budget",
                    )
                )
        elif op == "select.rendered_dom_records/v1":
            outputs[node_id] = {"records": "RenderedRecords"}
        elif op == "extract.fields/v1":
            extraction_seen = True
            outputs[node_id] = {"records": "TypedRecords"}
            for field_name, field in operation["fields"].items():
                field_path = f"{op_path}.fields.{field_name}"
                if field["source"] == "attribute" and not field.get("attribute"):
                    diagnostics.append(
                        Diagnostic(
                            "FIELD_MAPPING_INVALID",
                            field_path,
                            "attribute extraction requires an attribute name",
                        )
                    )
                if field["source"] == "json_value" and not field.get("path"):
                    diagnostics.append(
                        Diagnostic(
                            "FIELD_MAPPING_INVALID",
                            field_path,
                            "JSON extraction requires a restricted path",
                        )
                    )
                if field["source"] != "attribute" and field.get("attribute"):
                    diagnostics.append(
                        Diagnostic(
                            "FIELD_MAPPING_INVALID",
                            field_path,
                            "attribute is only valid for attribute extraction",
                        )
                    )
                if field["source"] != "json_value" and field.get("path"):
                    diagnostics.append(
                        Diagnostic(
                            "FIELD_MAPPING_INVALID",
                            field_path,
                            "path is only valid for JSON value extraction",
                        )
                    )
                if field["source"] == "json_value" and field.get("path"):
                    field_path_value = field["path"]
                    segments = (
                        field_path_value.split("/")[1:]
                        if field_path_value.startswith("/")
                        else []
                    )
                    if (
                        not field_path_value.startswith("/")
                        or "*" in segments
                        or any(
                            "~" in segment and re.search(r"~(?![01])", segment)
                            for segment in segments
                        )
                    ):
                        diagnostics.append(
                            Diagnostic(
                                "FIELD_MAPPING_INVALID",
                                field_path,
                                "field path must be a restricted JSON pointer without wildcards",
                            )
                        )
            contract = document["output_contract"]["fields"]
            if set(operation["fields"]) != set(contract):
                diagnostics.append(
                    Diagnostic(
                        "OUTPUT_CONTRACT_MISMATCH",
                        f"{op_path}.fields",
                        "extracted fields must exactly match output contract fields",
                    )
                )
            for field_name, contract_field in contract.items():
                field = operation["fields"].get(field_name)
                if field and (
                    field["type"] != contract_field["type"]
                    or field["required"] != contract_field["required"]
                ):
                    diagnostics.append(
                        Diagnostic(
                            "OUTPUT_CONTRACT_MISMATCH",
                            f"{op_path}.fields.{field_name}",
                            "field type/required status differs from output contract",
                        )
                    )
        elif op == "output.records/v1":
            output_seen = True
            outputs[node_id] = {"report": "ExecutionReport"}
        elif op == "output.raw/v1":
            outputs[node_id] = {"references": "RawReferences"}

        for name, binding in _bindings(operation):
            producer = by_id.get(binding["operation"])
            if producer is None:
                diagnostics.append(
                    Diagnostic(
                        "BINDING_INVALID",
                        f"{op_path}.{name}",
                        f"unknown operation {binding['operation']!r}",
                    )
                )
                continue
            dependencies[node_id].add(producer["id"])
            port_type = outputs.get(producer["id"], {}).get(binding["port"])
            if port_type is None:
                # Producer can occur later in document; resolve once all outputs exist below.
                continue

    # Resolve bindings after all output ports have been collected.
    type_rules = {
        "links.discover/v1": {"document": {"HtmlDocument"}},
        "pagination.bounded/v1": {"links": {"LinkSet"}},
        "browser.render_dom/v1": {"document": {"HtmlDocument", "JsonDocument"}},
        "select.html_records/v1": {"documents": {"HtmlDocument", "PageSet"}},
        "select.json_records/v1": {"document": {"JsonDocument"}},
        "select.rendered_dom_records/v1": {"document": {"RenderedDocument"}},
        "extract.fields/v1": {
            "records": {"HtmlRecords", "JsonRecords", "RenderedRecords"}
        },
        "output.records/v1": {"records": {"TypedRecords"}},
        "output.raw/v1": {"documents": {"HtmlDocument", "JsonDocument", "PageSet"}},
    }
    for index, operation in enumerate(operations):
        op_path = f"$.operations[{index}]"
        for name, binding in _bindings(operation):
            producer = by_id.get(binding["operation"])
            if producer is None:
                continue
            port_type = outputs.get(producer["id"], {}).get(binding["port"])
            if port_type is None:
                diagnostics.append(
                    Diagnostic(
                        "OUTPUT_PORT_UNKNOWN",
                        f"{op_path}.{name}.port",
                        f"operation {producer['id']!r} has no output port {binding['port']!r}",
                    )
                )
                continue
            allowed = type_rules.get(operation["op"], {}).get(name)
            if allowed is not None and port_type not in allowed:
                diagnostics.append(
                    Diagnostic(
                        "TYPE_MISMATCH",
                        f"{op_path}.{name}",
                        f"expected {sorted(allowed)}, got {port_type}",
                    )
                )

    for index, operation in enumerate(operations):
        required_refs = set(operation.get("evidence_refs", []))
        for ref in required_refs:
            if ref not in evidence:
                diagnostics.append(
                    Diagnostic(
                        "EVIDENCE_REFERENCE_MISSING",
                        f"$.operations[{index}].evidence_refs",
                        f"unknown evidence ref {ref!r}",
                    )
                )
        if operation["op"] in {"select.html_records/v1", "select.json_records/v1"} and (
            not required_refs or not required_refs <= set(evidence)
        ):
            evidence_sufficient = False
        if operation["op"] in {"select.html_records/v1", "links.discover/v1"}:
            try:
                from .reference_runtime import RuntimeFailure, _html

                selector = operation["selector"]
                fixtures = [
                    page
                    for page in fixture_pages.values()
                    if page.get("evidence_ref") in required_refs
                    and page.get("content_type") == "text/html"
                ]
                observed_matches = 0
                for fixture in fixtures:
                    observed_matches += len(_html(fixture["body"]).css(selector))
                if fixture_root is not None and observed_matches == 0:
                    evidence_sufficient = False
                    diagnostics.append(
                        Diagnostic(
                            "SELECTOR_NO_MATCHES",
                            f"$.operations[{index}].selector",
                            "selector has no matches in its linked evidence fixtures",
                        )
                    )
                if (
                    operation["op"] == "select.html_records/v1"
                    and operation["cardinality"] == "one"
                    and fixture_root is not None
                    and observed_matches != 1
                ):
                    diagnostics.append(
                        Diagnostic(
                            "CARDINALITY_INVALID",
                            f"$.operations[{index}].cardinality",
                            f"expected exactly one observed match, got {observed_matches}",
                        )
                    )
                if (
                    operation["op"] == "select.html_records/v1"
                    and operation["cardinality"] == "optional_one"
                    and fixture_root is not None
                    and observed_matches > 1
                ):
                    diagnostics.append(
                        Diagnostic(
                            "CARDINALITY_INVALID",
                            f"$.operations[{index}].cardinality",
                            f"expected at most one observed match, got {observed_matches}",
                        )
                    )
            except (RuntimeFailure, SelectolaxError, ValueError) as error:
                diagnostics.append(
                    Diagnostic(
                        "SELECTOR_INVALID",
                        f"$.operations[{index}].selector",
                        str(error),
                    )
                )
                evidence_sufficient = False
        if operation["op"] == "select.json_records/v1":
            path = operation["array_path"]
            segments = path.split("/")[1:]
            if segments.count("*") != 1 or not segments or segments[-1] != "*":
                diagnostics.append(
                    Diagnostic(
                        "JSON_PATH_INVALID",
                        f"$.operations[{index}].array_path",
                        "array path permits one final /* expansion",
                    )
                )
            elif any(
                "~" in segment and re.search(r"~(?![01])", segment)
                for segment in segments
            ):
                diagnostics.append(
                    Diagnostic(
                        "JSON_PATH_INVALID",
                        f"$.operations[{index}].array_path",
                        "invalid JSON Pointer escape",
                    )
                )
            else:
                try:
                    from .reference_runtime import RuntimeFailure, _json_path

                    fixtures = [
                        page
                        for page in fixture_pages.values()
                        if page.get("evidence_ref") in required_refs
                        and page.get("content_type") == "application/json"
                    ]
                    matches = 0
                    for fixture in fixtures:
                        value = json.loads(fixture["body"])
                        matches += len(_json_path(value, path, expand=True))
                    if fixture_root is not None and matches == 0:
                        evidence_sufficient = False
                        diagnostics.append(
                            Diagnostic(
                                "JSON_PATH_NO_MATCHES",
                                f"$.operations[{index}].array_path",
                                "array path has no matches in linked JSON evidence",
                            )
                        )
                except (
                    RuntimeFailure,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                    ValueError,
                ) as error:
                    evidence_sufficient = False
                    diagnostics.append(
                        Diagnostic(
                            "JSON_PATH_INVALID",
                            f"$.operations[{index}].array_path",
                            str(error),
                        )
                    )
        if operation["op"] == "extract.fields/v1":
            binding = operation["records"]
            producer = by_id.get(binding["operation"])
            producer_type = ""
            if producer:
                producer_type = {
                    "select.html_records/v1": "html",
                    "select.json_records/v1": "json",
                    "select.rendered_dom_records/v1": "rendered",
                }.get(producer["op"], "")
            for field_name, field in operation["fields"].items():
                source_kind = field["source"]
                valid_source = (
                    (producer_type == "html" and source_kind in {"text", "attribute"})
                    or (producer_type == "json" and source_kind == "json_value")
                    or (
                        producer_type == "rendered"
                        and source_kind in {"text", "attribute"}
                    )
                )
                if not valid_source:
                    diagnostics.append(
                        Diagnostic(
                            "FIELD_MAPPING_INVALID",
                            f"$.operations[{index}].fields.{field_name}.source",
                            f"{source_kind} is incompatible with {producer_type or 'unknown'} records",
                        )
                    )
            if (
                fixture_root is not None
                and producer_type in {"html", "json"}
                and producer is not None
            ):
                try:
                    from decimal import Decimal

                    from .reference_runtime import (
                        RuntimeFailure,
                        _convert,
                        _html,
                        _json_path,
                        _raw_value,
                    )

                    selected_records = []
                    selector_refs = set(producer.get("evidence_refs", []))
                    fixtures = [
                        page
                        for page in fixture_pages.values()
                        if page.get("evidence_ref") in selector_refs
                        and page.get("content_type")
                        == (
                            "text/html"
                            if producer_type == "html"
                            else "application/json"
                        )
                    ]
                    for fixture in fixtures:
                        page_ref = {
                            "url": fixture["url"],
                            "evidence_ref": fixture["evidence_ref"],
                            "sha256": fixture["sha256"],
                        }
                        if producer_type == "html":
                            nodes = _html(fixture["body"]).css(producer["selector"])
                            selected_records.extend(
                                {
                                    "kind": "html",
                                    "node": node,
                                    "page": page_ref,
                                }
                                for node in nodes
                            )
                        else:
                            value = json.loads(fixture["body"], parse_float=Decimal)
                            selected_records.extend(
                                {
                                    "kind": "json",
                                    "value": item,
                                    "page": page_ref,
                                }
                                for item, _ in _json_path(
                                    value, producer["array_path"], expand=True
                                )
                            )
                    if len(selected_records) > document["budgets"]["max_records"]:
                        diagnostics.append(
                            Diagnostic(
                                "BUDGET_OVERFLOW",
                                f"$.operations[{index}].records",
                                "fixture extraction exceeds max_records",
                            )
                        )
                    for record_index, record in enumerate(selected_records):
                        for field_name, field in operation["fields"].items():
                            try:
                                value = _raw_value(record, field)
                                _convert(value, field, field_name)
                            except RuntimeFailure as error:
                                evidence_sufficient = False
                                diagnostics.append(
                                    Diagnostic(
                                        error.code,
                                        f"$.operations[{index}].fields.{field_name}",
                                        f"fixture record {record_index}: {error}",
                                    )
                                )
                except (
                    RuntimeFailure,
                    SelectolaxError,
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValueError,
                    RecursionError,
                ) as error:
                    evidence_sufficient = False
                    diagnostics.append(
                        Diagnostic(
                            "FIELD_MAPPING_INVALID",
                            f"$.operations[{index}].fields",
                            f"could not inspect selected fixture records: {error}",
                        )
                    )

    static_pages = sum(
        1 for operation in operations if operation["op"] == "http.fetch/v1"
    ) + sum(
        operation.get("max_pages", 1) - 1
        for operation in operations
        if operation["op"] == "pagination.bounded/v1"
    )
    if static_pages > document["budgets"]["max_pages"]:
        diagnostics.append(
            Diagnostic(
                "BUDGET_OVERFLOW",
                "$.budgets.max_pages",
                "declared fetch and pagination maxima exceed the page budget",
            )
        )
    if static_pages > document["budgets"]["max_requests"]:
        diagnostics.append(
            Diagnostic(
                "BUDGET_OVERFLOW",
                "$.budgets.max_requests",
                "declared fetch and pagination maxima exceed the request budget",
            )
        )
    declared_capabilities = set(document["capabilities"])
    if inferred_capabilities != declared_capabilities:
        missing_capabilities = inferred_capabilities - declared_capabilities
        unused_capabilities = declared_capabilities - inferred_capabilities
        diagnostics.append(
            Diagnostic(
                "CAPABILITY_MISMATCH",
                "$.capabilities",
                f"missing={sorted(missing_capabilities)} unused={sorted(unused_capabilities)}",
            )
        )
    if not extraction_seen or not output_seen:
        diagnostics.append(
            Diagnostic(
                "OUTPUT_PIPELINE_INCOMPLETE",
                "$.operations",
                "v2 requires extract.fields and output.records operations",
            )
        )

    # Topological sort detects cycles without accepting arbitrary loops.
    visiting: set[str] = set()
    visited: set[str] = set()
    order: list[str] = []

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            diagnostics.append(
                Diagnostic(
                    "DAG_CYCLE", "$.operations", f"cycle reaches operation {node!r}"
                )
            )
            return
        visiting.add(node)
        for dependency in sorted(dependencies[node]):
            visit(dependency)
        visiting.remove(node)
        visited.add(node)
        order.append(node)

    for operation in operations:
        visit(operation["id"])
    sinks = {
        item["id"]
        for item in operations
        if item["op"] in {"output.records/v1", "output.raw/v1"}
    }
    reachable = set(sinks)
    changed = True
    while changed:
        changed = False
        for node in tuple(reachable):
            for dependency in dependencies[node]:
                if dependency not in reachable:
                    reachable.add(dependency)
                    changed = True
    for index, operation in enumerate(operations):
        if operation["id"] not in reachable:
            diagnostics.append(
                Diagnostic(
                    "OPERATION_UNREACHABLE",
                    f"$.operations[{index}]",
                    f"operation {operation['id']!r} does not contribute to an output",
                )
            )

    return ValidationResult(
        not diagnostics,
        tuple(sorted(diagnostics, key=lambda d: (d.path, d.code))),
        evidence_sufficient,
    )


def _bindings(operation: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    return [
        (key, value)
        for key, value in operation.items()
        if key in {"document", "documents", "links", "records"}
        and isinstance(value, dict)
        and set(value) == {"operation", "port"}
    ]


def build_typed_ir(
    parsed: ParsedPlaybook | dict[str, Any], *, fixture_root: Path | None = None
) -> TypedIR:
    document = parsed.document if isinstance(parsed, ParsedPlaybook) else parsed
    result = validate_playbook_v2(document, fixture_root=fixture_root)
    if not result.valid:
        summary = "; ".join(
            f"{item.code} {item.path}: {item.message}" for item in result.diagnostics
        )
        raise PlaybookError(summary)
    operations = document["operations"]
    by_id = {item["id"]: item for item in operations}
    ordered = []
    remaining = set(by_id)
    while remaining:
        ready = sorted(
            node
            for node in remaining
            if all(
                binding["operation"] not in remaining
                for _, binding in _bindings(by_id[node])
            )
        )
        if not ready:
            raise PlaybookError("[DAG_CYCLE] operation graph contains a cycle")
        for node in ready:
            operation = by_id[node]
            dependencies = sorted(
                {binding["operation"] for _, binding in _bindings(operation)}
            )
            ordered.append(
                {
                    "operation_id": node,
                    "operator_type": operation["op"].split("/", 1)[0],
                    "operator_version": operation["op"].split("/", 1)[1],
                    "inputs": {
                        key: value
                        for key, value in operation.items()
                        if key
                        in {
                            "url",
                            "content_type",
                            "selector",
                            "attribute",
                            "max_links",
                            "max_pages",
                            "stop_when_no_links",
                            "wait_policy",
                            "documents",
                            "document",
                            "links",
                            "records",
                            "cardinality",
                            "max_matches",
                            "array_path",
                            "fields",
                            "evidence_refs",
                        }
                    },
                    "dependencies": dependencies,
                    "input_types": _ir_input_types(operation, by_id),
                    "output_types": _ir_output_types(operation),
                    "capability_requirements": [CAPABILITY_BY_OP[operation["op"]]],
                    "budgets": dict(document["budgets"]),
                    "evidence_refs": sorted(operation.get("evidence_refs", [])),
                    "failure_policy": dict(document["failure_policy"]),
                }
            )
            remaining.remove(node)
    return TypedIR(
        {
            "ir_version": IR_VERSION,
            "playbook_id": document["playbook_id"],
            "source": document["source"],
            "capabilities": sorted(
                set(document["capabilities"]),
                key=lambda cap: _CAPABILITY_ORDER.index(cap),
            ),
            "budgets": document["budgets"],
            "failure_policy": document["failure_policy"],
            "output_contract": document["output_contract"],
            "nodes": ordered,
        }
    )


def _ir_input_types(
    operation: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> dict[str, str]:
    result = {}
    for name, binding in _bindings(operation):
        producer = by_id[binding["operation"]]
        result[name] = _ir_output_types(producer).get(binding["port"], "Unknown")
    return result


def _ir_output_types(operation: dict[str, Any]) -> dict[str, str]:
    if operation["op"] == "http.fetch/v1":
        return {
            "document": "HtmlDocument"
            if operation["content_type"] == "text/html"
            else "JsonDocument"
        }
    return {
        "links.discover/v1": {"links": "LinkSet"},
        "pagination.bounded/v1": {"documents": "PageSet"},
        "browser.render_dom/v1": {"rendered_document": "RenderedDocument"},
        "select.html_records/v1": {"records": "HtmlRecords"},
        "select.json_records/v1": {"records": "JsonRecords"},
        "select.rendered_dom_records/v1": {"records": "RenderedRecords"},
        "extract.fields/v1": {"records": "TypedRecords"},
        "output.records/v1": {"report": "ExecutionReport"},
        "output.raw/v1": {"references": "RawReferences"},
    }[operation["op"]]
