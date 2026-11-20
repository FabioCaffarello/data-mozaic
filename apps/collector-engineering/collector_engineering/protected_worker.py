"""Fixed, stdin-only worker for the trusted Scout MCP replay broker."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

from .playbooks import PlaybookError, accept_acquisition, load_playbook, replay

MAX_REQUEST_BYTES = 48_000_000
MAX_PAGES = 20
MAX_BODY_BYTES = 2_000_000
MAX_DRAFT_BYTES = 64_000
MAX_TRANSCRIPT_BYTES = 256_000
MAX_EXPECTATION_BYTES = 256_000


def _read_request() -> dict:
    request_bytes = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(request_bytes) > MAX_REQUEST_BYTES:
        raise ValueError("executor request exceeds the size limit")
    request = json.loads(request_bytes)
    if not isinstance(request, dict) or set(request) != {
        "action",
        "run_id",
        "draft_yaml",
        "transcript",
        "fixtures",
        "expectations",
    }:
        raise ValueError("executor request has an invalid shape")
    if request["action"] not in {"replay", "accept"}:
        raise ValueError("executor action is unsupported")
    if not isinstance(request["run_id"], str) or not re.fullmatch(
        r"[a-f0-9]{32}", request["run_id"]
    ):
        raise ValueError("executor run reference is invalid")
    if (
        not isinstance(request["draft_yaml"], str)
        or len(request["draft_yaml"].encode("utf-8")) > MAX_DRAFT_BYTES
    ):
        raise ValueError("playbook draft exceeds the size limit")
    if len(json.dumps(request["transcript"]).encode("utf-8")) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("transcript exceeds the size limit")
    if len(json.dumps(request["expectations"]).encode("utf-8")) > MAX_EXPECTATION_BYTES:
        raise ValueError("expectations exceed the size limit")
    pages = request["fixtures"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError("fixture page count is outside the allowed range")
    total = 0
    for page in pages:
        if not isinstance(page, dict) or set(page) != {
            "url",
            "status",
            "content_type",
            "evidence_ref",
            "artifact_id",
            "sha256",
            "size_bytes",
            "body_base64",
            "requested_url",
            "sequence",
            "request_count",
        }:
            raise ValueError("fixture page has an invalid shape")
        body = base64.b64decode(page["body_base64"], validate=True)
        total += len(body)
        if (
            len(body) > MAX_BODY_BYTES
            or page["size_bytes"] != len(body)
            or hashlib.sha256(body).hexdigest() != page["sha256"]
        ):
            raise ValueError("fixture bytes failed size or digest validation")
    if total > MAX_REQUEST_BYTES // 2:
        raise ValueError("fixture bytes exceed the aggregate limit")
    return request


def _write_fixtures(pages: list[dict], fixture_root: Path) -> None:
    fixture_root.mkdir(mode=0o700)
    index_pages = []
    for page in pages:
        body = base64.b64decode(page["body_base64"], validate=True)
        filename = f"{page['sha256']}.body"
        (fixture_root / filename).write_bytes(body)
        index_pages.append(
            {
                "url": page["url"],
                "requested_url": page["requested_url"],
                "effective_url": page["url"],
                "sequence": page["sequence"],
                "request_count": page["request_count"],
                "status": page["status"],
                "content_type": page["content_type"],
                "evidence_ref": page["evidence_ref"],
                "sha256": page["sha256"],
                "body_file": filename,
            }
        )
    (fixture_root / "index.json").write_text(
        json.dumps({"complete": True, "pages": index_pages}, sort_keys=True),
        encoding="utf-8",
    )


def _execute(request: dict) -> dict:
    results_root = Path("/results")
    output_root = results_root / request["run_id"]
    if output_root.exists():
        raise ValueError("run reference already exists")
    output_root.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="bc15-input-") as temporary:
        fixture_root = Path(temporary) / "fixtures"
        _write_fixtures(request["fixtures"], fixture_root)
        draft_path = Path(temporary) / "draft.yaml"
        draft_path.write_text(request["draft_yaml"], encoding="utf-8")
        draft = load_playbook(draft_path)
        if request["action"] == "replay":
            result = replay(draft, fixture_root, output_root)
            accepted = result.get("state") == "passed"
        else:
            result = accept_acquisition(
                draft,
                request["transcript"],
                fixture_root,
                output_root,
                request["expectations"],
            )
            accepted = (
                result.get("replay_execution", {}).get("state") == "passed"
                and result.get("acquisition_acceptance", {}).get("state") == "accepted"
            )
    if not accepted:
        raise PlaybookError("BC15 replay or acquisition acceptance failed")
    report_path = output_root / "report.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return {"state": "passed", "report": result}


def main() -> int:
    try:
        response = _execute(_read_request())
        print(json.dumps(response, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, PlaybookError, json.JSONDecodeError) as error:
        print(
            json.dumps(
                {
                    "state": "failed",
                    "error": {"code": "replay_failed", "message": str(error)},
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
