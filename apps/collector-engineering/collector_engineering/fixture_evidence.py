"""Bounded inspection of offline fixture evidence for BC15 validation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .playbook_v2 import Diagnostic
from .syntax import strict_json_loads


def inspect_fixture_evidence(
    fixture_root: Path | None,
) -> tuple[dict[str, dict[str, Any]], list[Diagnostic]]:
    """Read and verify the bounded fixture evidence index.

    This module owns filesystem and byte inspection only. Playbook semantics,
    evidence binding, and capability validation stay with BC15's validator.
    """
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
    if any(part.is_symlink() for part in (fixture_root, *fixture_root.parents)):
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
        index = strict_json_loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as error:
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
    fixture_source = index.get("source")
    if (
        not isinstance(fixture_source, dict)
        or not isinstance(fixture_source.get("source_id"), str)
        or not fixture_source.get("source_id")
        or not isinstance(fixture_source.get("revision"), str)
        or not fixture_source.get("revision")
    ):
        return {}, [
            Diagnostic(
                "FIXTURE_SOURCE_INVALID",
                "$.fixtures.source",
                "fixture index must declare source_id and revision",
            )
        ]

    page_map: dict[str, dict[str, Any]] = {}
    diagnostics: list[Diagnostic] = []
    total_bytes = 0
    for number, page in enumerate(index["pages"]):
        path = f"$.fixtures[{number}]"
        if not isinstance(page, dict):
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_INDEX_INVALID",
                    path,
                    "fixture page entry must be an object",
                )
            )
            continue
        body_name = page.get("body_file", "")
        if not isinstance(body_name, str) or not body_name:
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_PATH_UNSAFE",
                    path,
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
            body_relative.is_absolute()
            or ".." in body_relative.parts
            or "\\" in body_name
            or unsafe_component
        ):
            diagnostics.append(
                Diagnostic("FIXTURE_PATH_UNSAFE", path, "fixture body path is unsafe")
            )
            continue
        try:
            if not body_path.is_file():
                raise OSError("fixture body is not a regular file")
            if body_path.stat().st_size > 2_000_000:
                raise OSError("fixture body exceeds 2 MB")
            body = body_path.read_bytes()
        except OSError as error:
            diagnostics.append(Diagnostic("FIXTURE_BODY_MISSING", path, str(error)))
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
        if (
            not isinstance(declared_size, int)
            or isinstance(declared_size, bool)
            or declared_size != len(body)
        ):
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_SIZE_MISMATCH",
                    f"{path}.size_bytes",
                    "declared fixture size differs from the body",
                )
            )
            continue
        if (
            not isinstance(page.get("status"), int)
            or isinstance(page.get("status"), bool)
            or not isinstance(page.get("content_type"), str)
            or not isinstance(page.get("evidence_ref"), str)
            or not page.get("evidence_ref")
        ):
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_METADATA_INVALID",
                    path,
                    "fixture page must declare integer status, content_type, and evidence_ref",
                )
            )
            continue
        digest = hashlib.sha256(body).hexdigest()
        if digest != page.get("sha256"):
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_DIGEST_MISMATCH",
                    f"{path}.sha256",
                    f"expected {page.get('sha256')}, got {digest}",
                )
            )
            continue
        url = page.get("url", "")
        if not isinstance(url, str) or not url:
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_INDEX_INVALID",
                    f"{path}.url",
                    "fixture URL must be a non-empty string",
                )
            )
            continue
        if url in page_map:
            diagnostics.append(
                Diagnostic(
                    "FIXTURE_URL_DUPLICATE",
                    f"{path}.url",
                    f"fixture URL {url!r} occurs more than once",
                )
            )
            continue
        page_map[url] = {
            **page,
            "body": body,
            "body_path": body_path,
            "source_id": fixture_source["source_id"],
            "source_revision": fixture_source["revision"],
        }
    return page_map, diagnostics
