"""Verify worker declarations against trusted gateway evidence before import."""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .plan_validation import verify_plan_results
from .result_bundle import (
    MAX_REPORT_BYTES,
    MAX_RESULT_BYTES,
    FixtureImportError,
    VerifiedFixturePage,
    VerifiedResultBundle,
    _read_regular,
    _strict_json,
)


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise FixtureImportError("gateway acquisition timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise FixtureImportError("gateway acquisition timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FixtureImportError("gateway acquisition timestamp must include timezone")
    parsed = parsed.astimezone(UTC)
    if parsed > datetime.now(UTC):
        raise FixtureImportError("gateway acquisition timestamp is in the future")
    return parsed


def _fetch_trusted_evidence(
    *,
    endpoint: str,
    token: str,
    fetch_id: str,
    tenant_id: str,
    run_id: str,
    attempt_id: str,
    generation: int,
    max_body_bytes: int,
) -> tuple[dict[str, object], bytes]:
    if re.fullmatch(r"[0-9a-f]{32}", fetch_id) is None:
        raise FixtureImportError("gateway fetch identity is invalid")
    request = Request(
        endpoint.rstrip("/") + "/internal/v1/evidence/" + fetch_id,
        headers={
            "Authorization": "Bearer " + token,
            "X-DM-Tenant-ID": tenant_id,
            "X-DM-Run-ID": run_id,
            "X-DM-Attempt-ID": attempt_id,
            "X-DM-Generation": str(generation),
        },
    )
    try:
        with build_opener(_RejectRedirects).open(request, timeout=10) as response:
            if response.status != 200:
                raise FixtureImportError("gateway evidence lookup was rejected")
            encoded = response.headers.get("X-Mozaic-Evidence", "")
            body = response.read(max_body_bytes + 1)
            if len(body) > max_body_bytes or response.read(1):
                raise FixtureImportError("gateway evidence body exceeds its bound")
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise FixtureImportError("trusted gateway evidence is unavailable") from error
    if not encoded or len(encoded) > 12_000:
        raise FixtureImportError("gateway evidence metadata is invalid")
    try:
        metadata_bytes = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as error:
        raise FixtureImportError(
            "gateway evidence metadata encoding is invalid"
        ) from error
    metadata = _strict_json(metadata_bytes, "trusted gateway evidence")
    if not isinstance(metadata, dict):
        raise FixtureImportError("trusted gateway evidence must be an object")
    expected_fields = {
        "contract_version",
        "fetch_id",
        "tenant_id",
        "run_id",
        "attempt_id",
        "generation",
        "source_id",
        "source_revision_id",
        "policy_digest",
        "release_id",
        "build_manifest_digest",
        "request_target_digest",
        "effective_target_digest",
        "request_origin",
        "effective_origin",
        "request_path_digest",
        "effective_path_digest",
        "query_parameter_names",
        "redirect_count",
        "status",
        "acquired_at",
        "media_type",
        "body_representation",
        "body_size",
        "body_digest",
        "body_ref",
    }
    if (
        set(metadata) != expected_fields
        or metadata.get("contract_version") != "fetch-evidence/v1"
        or metadata.get("fetch_id") != fetch_id
        or metadata.get("tenant_id") != tenant_id
        or metadata.get("run_id") != run_id
        or metadata.get("attempt_id") != attempt_id
        or metadata.get("generation") != generation
        or metadata.get("body_ref") != fetch_id + ".body"
        or metadata.get("body_size") != len(body)
        or isinstance(metadata.get("body_size"), bool)
        or metadata.get("body_digest") != _digest(body)
        or metadata.get("body_representation") != "decoded-http-entity"
        or not isinstance(metadata.get("query_parameter_names"), list)
        or any(not isinstance(name, str) for name in metadata["query_parameter_names"])
        or not isinstance(metadata.get("redirect_count"), int)
        or isinstance(metadata.get("redirect_count"), bool)
        or metadata.get("redirect_count", -1) < 0
        or not isinstance(metadata.get("status"), int)
        or isinstance(metadata.get("status"), bool)
        or not isinstance(metadata.get("source_id"), str)
        or not isinstance(metadata.get("source_revision_id"), str)
        or not isinstance(metadata.get("policy_digest"), str)
        or not isinstance(metadata.get("release_id"), str)
        or not isinstance(metadata.get("build_manifest_digest"), str)
        or not isinstance(metadata.get("request_target_digest"), str)
        or not isinstance(metadata.get("effective_target_digest"), str)
        or not isinstance(metadata.get("request_origin"), str)
        or not isinstance(metadata.get("effective_origin"), str)
        or not isinstance(metadata.get("request_path_digest"), str)
        or not isinstance(metadata.get("effective_path_digest"), str)
        or not isinstance(metadata.get("media_type"), str)
    ):
        raise FixtureImportError("gateway evidence bytes or execution binding diverged")
    return metadata, body


def _plan_pagination_urls(
    plan: dict[str, object],
    pages: list[VerifiedFixturePage],
    reported: object,
    *,
    expected_initial_url: str | None = None,
) -> None:
    from collector_engineering import reference_runtime

    fetch = plan.get("fetch")
    discover = plan.get("discover")
    pagination = plan.get("pagination")
    if not isinstance(fetch, dict) or not isinstance(fetch.get("inputs"), dict):
        raise FixtureImportError("compiled fetch plan is malformed")
    expected_url = expected_initial_url or fetch["inputs"].get("url")
    if not isinstance(expected_url, str):
        raise FixtureImportError("compiled fetch URL is invalid")
    if discover is None:
        if len(pages) != 1 or reported not in {None, "natural_end"}:
            raise FixtureImportError("non-paginated plan returned multiple pages")
        if pages[0].url != expected_url:
            raise FixtureImportError("live fetch differs from compiled plan URL")
        return
    if not isinstance(discover, dict) or not isinstance(discover.get("inputs"), dict):
        raise FixtureImportError("compiled pagination discovery is malformed")
    discovery = discover["inputs"]
    if not isinstance(pagination, dict) or not isinstance(
        pagination.get("inputs"), dict
    ):
        raise FixtureImportError("compiled pagination budget is missing")
    page_policy = pagination["inputs"]
    mode = page_policy.get("completion_mode", "natural_end")
    maximum = page_policy.get("max_pages")
    if mode not in {"natural_end", "bounded_window"} or not isinstance(maximum, int):
        raise FixtureImportError("compiled pagination completion policy is invalid")
    if len(pages) > maximum:
        raise FixtureImportError("live fetch exceeded the compiled page budget")
    for index, page in enumerate(pages):
        document = {
            "url": page.url,
            "body": page.body,
            "evidence_ref": page.evidence_ref,
            "page_index": index,
        }
        links = reference_runtime.discover_links(
            _plan_context(plan),
            document,
            selector=discovery["selector"],
            max_links=discovery["max_links"],
            evidence_refs=[page.evidence_ref],
        )["urls"]
        if index + 1 < len(pages):
            if len(links) != 1 or links[0] != pages[index + 1].url:
                raise FixtureImportError(
                    "live pagination chain differs from trusted bodies"
                )
        elif mode == "natural_end":
            if reported != "natural_end" or links:
                raise FixtureImportError("live pagination did not naturally terminate")
        elif mode == "bounded_window":
            if (
                reported != "planned_page_limit"
                or len(pages) != maximum
                or len(links) != 1
            ):
                raise FixtureImportError(
                    "live pagination did not complete its approved window"
                )
            if links[0] in {item.url for item in pages}:
                raise FixtureImportError("planned pagination continuation loops")
        else:
            raise FixtureImportError("compiled pagination mode is unsupported")


def _plan_context(plan: dict[str, object]):
    from types import SimpleNamespace

    source, budgets = plan.get("source"), plan.get("budgets")
    if not isinstance(source, dict) or not isinstance(budgets, dict):
        raise FixtureImportError("compiled plan source or budgets are malformed")
    return SimpleNamespace(source=source, budgets=budgets)


def _authorized_parameterized_target(
    plan: dict[str, object], parameters_json: str | None, parameters_digest: str | None
) -> str | None:
    """Derive the only parameterized target from BC04's immutable binding."""
    fetch = plan.get("fetch")
    if not isinstance(fetch, dict) or not isinstance(fetch.get("inputs"), dict):
        raise FixtureImportError("compiled fetch plan is malformed")
    inputs = fetch["inputs"]
    query_binding = inputs.get("input_query")
    if query_binding is None:
        if parameters_json is not None or parameters_digest is not None:
            raise FixtureImportError("BC04 parameters do not match the compiled plan")
        return None
    if (
        not isinstance(query_binding, dict)
        or set(query_binding) != {"parameter", "query_name"}
        or not isinstance(query_binding.get("parameter"), str)
        or not isinstance(query_binding.get("query_name"), str)
        or not isinstance(parameters_json, str)
        or not isinstance(parameters_digest, str)
    ):
        raise FixtureImportError("parameterized execution binding is incomplete")
    encoded_parameters = parameters_json.encode("utf-8", errors="strict")
    if (
        len(encoded_parameters) > 64_000
        or _digest(encoded_parameters) != parameters_digest
    ):
        raise FixtureImportError("BC04 parameter digest differs from its values")
    try:
        parameters = _strict_json(encoded_parameters, "BC04 input parameters")
    except FixtureImportError as error:
        raise FixtureImportError("BC04 parameter document is invalid") from error
    parameter_name = query_binding["parameter"]
    query_name = query_binding["query_name"]
    value = parameters.get(parameter_name) if isinstance(parameters, dict) else None
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {parameter_name}
        or isinstance(value, bool)
        or not isinstance(value, (str, int))
        or (
            isinstance(value, str)
            and (not value or len(value) > 128 or any(ord(c) < 0x20 for c in value))
        )
    ):
        raise FixtureImportError("BC04 parameters differ from the compiled input")
    raw_url = inputs.get("url")
    if not isinstance(raw_url, str):
        raise FixtureImportError("compiled input URL is invalid")
    parsed = urlsplit(raw_url)
    try:
        original_query = parse_qsl(
            parsed.query, keep_blank_values=True, strict_parsing=True
        )
    except ValueError as error:
        raise FixtureImportError("compiled input query is ambiguous") from error
    if (
        parsed.fragment
        or len(original_query) != 1
        or original_query[0][0] != query_name
    ):
        raise FixtureImportError("compiled input query differs from its binding")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode({query_name: value}),
            "",
        )
    )


def verify_live_result_bundle(
    result_dir: Path,
    *,
    tenant_id: str,
    run_id: str,
    attempt_id: str,
    generation: int,
    source_id: str,
    source_revision_id: str,
    policy_digest: str,
    release_id: str,
    build_manifest_digest: str,
    gateway_evidence_url: str,
    gateway_evidence_token: str,
    compiled_plan: dict[str, object],
    trusted_bot_id: str | None = None,
    max_response_bytes: int,
    maximum_total_response_bytes: int,
    maximum_requests: int = 50,
    execution_deadline: datetime | None = None,
    input_parameters_json: str | None = None,
    input_parameters_digest: str | None = None,
) -> VerifiedResultBundle:
    """Validate worker declarations against current gateway evidence and plan."""
    from collector_engineering import reference_runtime

    if (
        result_dir.is_symlink()
        or not result_dir.is_dir()
        or any(path.is_symlink() for path in (result_dir, result_dir.parent))
    ):
        raise FixtureImportError("live result directory is unsafe")
    expected_names = {
        "records.jsonl",
        "report.json",
        "fetch-evidence.jsonl",
        "raw-references.json",
    }
    entries = list(result_dir.iterdir())
    if {item.name for item in entries} != expected_names or any(
        item.is_symlink() or not item.is_file() for item in entries
    ):
        raise FixtureImportError("live result bundle membership is invalid")
    files = {
        name: _read_regular(
            result_dir / name,
            maximum=MAX_REPORT_BYTES if name == "report.json" else MAX_RESULT_BYTES,
            label="live " + name,
        )
        for name in expected_names
    }
    if sum(map(len, files.values())) > MAX_RESULT_BYTES:
        raise FixtureImportError("live result bundle exceeds its aggregate bound")
    report = _strict_json(files["report.json"], "live worker report")
    records_raw = files["records.jsonl"]
    records_digest = hashlib.sha256(records_raw).hexdigest()
    if (
        not isinstance(report, dict)
        or report.get("state") != "passed"
        or not isinstance(report.get("requests"), int)
        or not isinstance(report.get("pages"), int)
        or report.get("requests") < 1
        or report.get("pages") < 1
        or report.get("requests") != report.get("pages")
        or report.get("records_file") != "records.jsonl"
        or report.get("records_sha256") != records_digest
    ):
        raise FixtureImportError("live worker report does not match its records")
    records: list[dict[str, object]] = []
    for line in records_raw.splitlines(keepends=True):
        if not line.endswith(b"\n") or len(line) > 1_000_000:
            raise FixtureImportError("live worker record line is malformed")
        record = _strict_json(line[:-1], "live worker record")
        if not isinstance(record, dict):
            raise FixtureImportError("live worker record must be an object")
        records.append(record)
    if len(records) != report.get("records") or len(records) > 10_000:
        raise FixtureImportError("live record count differs from worker report")
    worker_evidence = []
    for line in files["fetch-evidence.jsonl"].splitlines():
        row = _strict_json(line, "worker-declared fetch evidence")
        if not isinstance(row, dict):
            raise FixtureImportError("worker fetch evidence must be an object")
        worker_evidence.append(row)
    raw_references = _strict_json(files["raw-references.json"], "worker raw references")
    if (
        not isinstance(raw_references, list)
        or len(raw_references) != len(worker_evidence)
        or not worker_evidence
        or len(worker_evidence) > maximum_requests
        or report.get("requests") != len(worker_evidence)
        or report.get("pages") != len(worker_evidence)
    ):
        raise FixtureImportError("worker completion count differs from fetch evidence")

    plan = dict(compiled_plan)
    source = plan.get("source")
    budgets = plan.get("budgets")
    select = plan.get("select")
    if (
        plan.get("profile") != "scrapy-live-http-v1"
        or not isinstance(source, dict)
        or source.get("source_id") != source_id
        or source.get("revision") != source_revision_id
        or not isinstance(budgets, dict)
        or len(worker_evidence)
        > min(int(budgets.get("max_requests", 0)), int(budgets.get("max_pages", 0)))
        or not isinstance(select, dict)
    ):
        raise FixtureImportError("live result differs from the trusted compiled plan")
    operator = select.get("operator_type")
    expected_media = (
        "text/html" if operator == "select.html_records" else "application/json"
    )
    trusted_pages: list[VerifiedFixturePage] = []
    seen_fetch_ids: set[str] = set()
    seen_refs: set[str] = set()
    aggregate_response_bytes = 0
    for worker_row in worker_evidence:
        fetch_id = worker_row.get("fetch_id")
        request_url = worker_row.get("source_url")
        if (
            not isinstance(fetch_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", fetch_id) is None
            or fetch_id in seen_fetch_ids
            or not isinstance(request_url, str)
        ):
            raise FixtureImportError("worker fetch identity is missing or duplicated")
        seen_fetch_ids.add(fetch_id)
        metadata, body = _fetch_trusted_evidence(
            endpoint=gateway_evidence_url,
            token=gateway_evidence_token,
            fetch_id=fetch_id,
            tenant_id=tenant_id,
            run_id=run_id,
            attempt_id=attempt_id,
            generation=generation,
            max_body_bytes=max_response_bytes,
        )
        aggregate_response_bytes += len(body)
        if aggregate_response_bytes > maximum_total_response_bytes:
            raise FixtureImportError("aggregate gateway response bytes exceed policy")
        media_type = metadata.get("media_type")
        if (
            metadata.get("source_id") != source_id
            or metadata.get("source_revision_id") != source_revision_id
            or metadata.get("policy_digest") != policy_digest
            or metadata.get("release_id") != release_id
            or metadata.get("build_manifest_digest") != build_manifest_digest
            or metadata.get("status") != 200
            or not isinstance(media_type, str)
            or media_type.split(";", 1)[0].lower() != expected_media
            or not reference_runtime._scoped(request_url, source)
        ):
            raise FixtureImportError(
                "trusted fetch evidence differs from admitted binding"
            )
        acquired_at = _time(metadata.get("acquired_at"))
        if execution_deadline is not None and acquired_at > execution_deadline:
            raise FixtureImportError(
                "gateway evidence was acquired after the run deadline"
            )
        if (
            metadata.get("request_target_digest")
            != _digest(request_url.encode("utf-8"))
            or metadata.get("request_origin")
            != urlsplit(request_url).scheme + "://" + urlsplit(request_url).netloc
            or metadata.get("request_path_digest")
            != _digest(urlsplit(request_url).path.encode("utf-8"))
            or metadata.get("body_size") != len(body)
            or isinstance(metadata.get("body_size"), bool)
            or metadata.get("body_digest") != _digest(body)
            or metadata.get("body_representation") != "decoded-http-entity"
            or not isinstance(metadata.get("redirect_count"), int)
            or isinstance(metadata.get("redirect_count"), bool)
            or metadata.get("redirect_count", -1) < 0
            or not isinstance(metadata.get("status"), int)
            or isinstance(metadata.get("status"), bool)
            or not isinstance(metadata.get("effective_path_digest"), str)
        ):
            raise FixtureImportError("gateway evidence target or body binding differs")
        declared_media = worker_row.get("media_type")
        if (
            worker_row.get("evidence_ref") != fetch_id
            or worker_row.get("body_sha256") != hashlib.sha256(body).hexdigest()
            or worker_row.get("body_size_bytes") != len(body)
            or declared_media != media_type.split(";", 1)[0].lower()
            or worker_row.get("body_representation")
            != metadata.get("body_representation")
            or _time(worker_row.get("acquired_at")) != acquired_at
        ):
            raise FixtureImportError(
                "worker evidence declaration diverges from gateway evidence"
            )
        matches = [
            item
            for item in raw_references
            if isinstance(item, dict) and item.get("evidence_ref") == fetch_id
        ]
        if len(matches) != 1:
            raise FixtureImportError("live fetch has no unique worker raw reference")
        raw = matches[0]
        if (
            raw.get("url") != request_url
            or raw.get("sha256") != hashlib.sha256(body).hexdigest()
            or raw.get("evidence_ref") in seen_refs
        ):
            raise FixtureImportError("live raw reference differs from trusted evidence")
        seen_refs.add(fetch_id)
        trusted_pages.append(
            VerifiedFixturePage(
                request_url,
                fetch_id,
                _digest(body),
                media_type,
                body,
                acquired_at,
                metadata["redirect_count"],
                str(metadata.get("effective_path_digest", "")),
            )
        )
    if len(seen_refs) != len(raw_references):
        raise FixtureImportError("worker raw references contain unknown evidence")
    parameterized_target = _authorized_parameterized_target(
        plan, input_parameters_json, input_parameters_digest
    )
    _plan_pagination_urls(
        plan,
        trusted_pages,
        report.get("pagination_termination"),
        expected_initial_url=parameterized_target,
    )
    verify_plan_results(
        plan=plan,
        pages=tuple(trusted_pages),
        records=tuple(records),
        source_id=source_id,
        source_revision=source_revision_id,
    )
    return VerifiedResultBundle(
        tuple(records),
        tuple(trusted_pages),
        records_digest,
        sum(map(len, files.values())),
        trusted_bot_id,
    )
