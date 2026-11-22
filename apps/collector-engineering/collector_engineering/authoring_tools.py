"""Bounded, metadata-first inspection helpers for synthetic BC15 fixtures."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from selectolax.lexbor import SelectolaxError

from .reference_runtime import RuntimeFailure, _html, _safe_directory

MAX_SAMPLE_COUNT = 5
MAX_SAMPLE_CHARS = 180
MAX_TOOL_OUTPUT_BYTES = 16_384
MAX_FIXTURE_BYTES = 2_000_000


def _load_fixture(fixture_root: Path, url: str) -> tuple[dict[str, Any], bytes]:
    root = _safe_directory(fixture_root, must_exist=True)
    index_path = root / "index.json"
    if index_path.is_symlink() or not index_path.is_file():
        raise RuntimeFailure("FIXTURE_INDEX_MISSING", "fixture index is missing")
    if index_path.stat().st_size > 1_000_000:
        raise RuntimeFailure("FIXTURE_INDEX_TOO_LARGE", "fixture index exceeds 1 MB")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeFailure("FIXTURE_INDEX_INVALID", str(error)) from error
    if (
        not isinstance(index, dict)
        or not isinstance(index.get("pages"), list)
        or len(index["pages"]) > 200
        or index.get("complete") is not True
        or any(not isinstance(item, dict) for item in index["pages"])
    ):
        raise RuntimeFailure(
            "FIXTURE_INDEX_INVALID",
            "fixture index must contain at most 200 page objects",
        )
    page = next(
        (item for item in index.get("pages", []) if item.get("url") == url), None
    )
    if page is None:
        raise RuntimeFailure("FIXTURE_NOT_FOUND", f"no fixture for URL {url}")
    if any(item.get("url") == url for item in index["pages"] if item is not page):
        raise RuntimeFailure(
            "FIXTURE_URL_DUPLICATE", f"fixture URL {url} is duplicated"
        )
    body_name = page.get("body_file", "")
    if not isinstance(body_name, str) or not body_name:
        raise RuntimeFailure("FIXTURE_PATH_UNSAFE", "fixture body path is invalid")
    body_relative = Path(body_name)
    if body_relative.is_absolute() or ".." in body_relative.parts or "\\" in body_name:
        raise RuntimeFailure(
            "FIXTURE_PATH_UNSAFE", "fixture path traversal is not allowed"
        )
    body_path = root / body_relative
    current = root
    unsafe_component = False
    for part in body_relative.parts:
        current = current / part
        unsafe_component = unsafe_component or current.is_symlink()
    if unsafe_component:
        raise RuntimeFailure("FIXTURE_PATH_UNSAFE", "fixture body cannot be a symlink")
    if not body_path.is_file() or body_path.stat().st_size > MAX_FIXTURE_BYTES:
        raise RuntimeFailure(
            "FIXTURE_TOO_LARGE", f"fixture exceeds {MAX_FIXTURE_BYTES} bytes"
        )
    body = body_path.read_bytes()
    if page.get("size_bytes") is not None and page["size_bytes"] != len(body):
        raise RuntimeFailure(
            "FIXTURE_SIZE_MISMATCH", "fixture size does not match index"
        )
    digest = hashlib.sha256(body).hexdigest()
    if digest != page.get("sha256"):
        raise RuntimeFailure(
            "FIXTURE_DIGEST_MISMATCH", f"fixture digest mismatch for {url}"
        )
    return page, body


def _bounded_json(value: Any, *, limit: int = MAX_TOOL_OUTPUT_BYTES) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > limit:
        raise RuntimeFailure("TOOL_OUTPUT_LIMIT", f"tool result exceeds {limit} bytes")
    return value


def inspect_fixture(fixture_root: Path, url: str) -> dict[str, Any]:
    page, body = _load_fixture(fixture_root, url)
    result: dict[str, Any] = {
        "url": page["url"],
        "status": page.get("status"),
        "content_type": page.get("content_type"),
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "evidence_ref": page.get("evidence_ref"),
        "content_values_omitted": True,
    }
    if page.get("content_type") == "text/html":
        parser = _html(body)
        counts = Counter(node.tag for node in parser.css("*"))
        result["structure"] = {
            "element_counts": dict(sorted(counts.items())),
            "sample_tags": sorted(counts)[:32],
        }
    elif page.get("content_type") == "application/json":
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise RuntimeFailure("JSON_DOCUMENT_INVALID", str(error)) from error
        result["structure"] = _json_shape(value)
    else:
        result["structure"] = {
            "kind": "unsupported",
            "content_type": page.get("content_type"),
        }
    return _bounded_json(result)


def _json_shape(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "…"
    if isinstance(value, dict):
        keys = sorted(value)[:50]
        shape = {key: _json_shape(value[key], depth + 1) for key in keys}
        if len(value) > len(keys):
            shape["…truncated_keys"] = len(value) - len(keys)
        return shape
    if isinstance(value, list):
        return {
            "array_length": len(value),
            "sample_shape": _json_shape(value[0], depth + 1) if value else None,
        }
    return {"scalar_type": "null" if value is None else type(value).__name__}


def try_selector(
    fixture_root: Path,
    url: str,
    selector: str,
    *,
    evidence_ref: str,
    max_samples: int = 3,
) -> dict[str, Any]:
    if not selector or len(selector) > 256:
        raise RuntimeFailure(
            "SELECTOR_INVALID", "selector must contain 1 to 256 characters"
        )
    page, body = _load_fixture(fixture_root, url)
    if page.get("content_type") != "text/html":
        raise RuntimeFailure(
            "CONTENT_TYPE_MISMATCH", "try-selector requires an HTML fixture"
        )
    if page.get("evidence_ref") != evidence_ref:
        raise RuntimeFailure(
            "EVIDENCE_REFERENCE_MISMATCH", "evidence ref does not identify this fixture"
        )
    if not 0 <= max_samples <= MAX_SAMPLE_COUNT:
        raise RuntimeFailure(
            "TOOL_SAMPLE_LIMIT", f"max_samples must be between 0 and {MAX_SAMPLE_COUNT}"
        )
    parser = _html(body)
    try:
        matches = parser.css(selector)
    except SelectolaxError as error:
        return _bounded_json(
            {
                "valid_selector": False,
                "diagnostic": {"code": "SELECTOR_INVALID", "message": str(error)},
                "match_count": 0,
                "samples": [],
                "evidence_ref": evidence_ref,
            }
        )
    samples = []
    for node in matches[:max_samples]:
        text = " ".join(node.text(deep=True, separator=" ").split())[:MAX_SAMPLE_CHARS]
        attrs = {
            key: str(value)[:MAX_SAMPLE_CHARS]
            for key, value in sorted(node.attributes.items())[:8]
        }
        samples.append(
            {
                "tag": node.tag,
                "text_sample": text,
                "attribute_samples": attrs,
                "untrusted_content": True,
            }
        )
    count = len(matches)
    return _bounded_json(
        {
            "valid_selector": True,
            "selector": selector,
            "match_count": count,
            "observed_cardinality": "zero"
            if count == 0
            else "one"
            if count == 1
            else "many",
            "samples": samples,
            "samples_truncated": count > len(samples),
            "evidence_ref": evidence_ref,
            "fixture_sha256": page["sha256"],
            "semantic_correctness_proven": False,
        }
    )
