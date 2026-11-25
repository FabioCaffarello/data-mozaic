"""Verify worker declarations against trusted gateway evidence before import."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from fixture_acquisition import (
    MAX_REPORT_BYTES,
    MAX_RESULT_BYTES,
    FixtureImportError,
    VerifiedFixturePage,
    VerifiedResultBundle,
    _read_regular,
    _strict_json,
)


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
        with urlopen(request, timeout=10) as response:
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
        or metadata.get("body_digest") != _digest(body)
        or metadata.get("body_representation") != "decoded-http-entity"
    ):
        raise FixtureImportError("gateway evidence bytes or execution binding diverged")
    return metadata, body


def _controlled_record_values(body: bytes, media_type: str) -> list[dict[str, object]]:
    """Map the two bounded controlled-test payloads; no general mapping engine."""
    if media_type == "text/html":

        class ProductParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.records: list[dict[str, str]] = []
                self.current: dict[str, str] | None = None
                self.field: str | None = None

            def handle_starttag(
                self, tag: str, attrs: list[tuple[str, str | None]]
            ) -> None:
                values = dict(attrs)
                article_classes = set(values.get("class", "").split())
                if tag == "article" and article_classes.intersection(
                    {"product", "item"}
                ):
                    if self.current is not None:
                        raise FixtureImportError(
                            "nested controlled HTML products are invalid"
                        )
                    self.current = {"id": values.get("data-id", "")}
                    self.current["_shape"] = (
                        "item" if "item" in article_classes else "product"
                    )
                    self.current["name" if "item" in article_classes else "title"] = ""
                    if "product" in article_classes:
                        self.current["price"] = ""
                elif (
                    self.current is not None
                    and tag == "h2"
                    and "title" in values.get("class", "").split()
                ):
                    self.field = "title"
                elif (
                    self.current is not None
                    and tag == "span"
                    and ("price" if self.current.get("_shape") == "product" else "name")
                    in values.get("class", "").split()
                ):
                    self.field = (
                        "price" if self.current.get("_shape") == "product" else "name"
                    )

            def handle_endtag(self, tag: str) -> None:
                if tag in {"h2", "span"}:
                    self.field = None
                elif tag == "article" and self.current is not None:
                    self.records.append(self.current)
                    self.current = None

            def handle_data(self, data: str) -> None:
                if self.current is not None and self.field is not None:
                    self.current[self.field] += data

        try:
            parser = ProductParser()
            parser.feed(body.decode("utf-8", errors="strict"))
            parser.close()
        except (UnicodeDecodeError, ValueError) as error:
            raise FixtureImportError("controlled HTML body is malformed") from error
        records = []
        for item in parser.records:
            shape = item.pop("_shape", None)
            expected_fields = (
                {"id", "name"} if shape == "item" else {"id", "title", "price"}
            )
            if set(item) != expected_fields or not all(item.values()):
                raise FixtureImportError("controlled HTML record fields are invalid")
            records.append({key: value.strip() for key, value in item.items()})
        if not records or parser.current is not None:
            raise FixtureImportError("controlled HTML body is incomplete")
        return records
    if media_type != "application/json":
        raise FixtureImportError("controlled response media type is unsupported")
    document = _strict_json(body, "controlled JSON response")
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise FixtureImportError(
            "controlled JSON body does not match its declared shape"
        )
    records: list[dict[str, object]] = []
    for item in document["items"]:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "quantity",
            "unit_price",
            "enabled",
        }:
            raise FixtureImportError("controlled JSON item fields are invalid")
        try:
            quantity = int(item["quantity"])
            price = format(float(item["unit_price"]), ".15g")
        except (TypeError, ValueError) as error:
            raise FixtureImportError(
                "controlled JSON numeric data is invalid"
            ) from error
        enabled = item["enabled"]
        if not isinstance(enabled, bool):
            if enabled not in {"true", "false"}:
                raise FixtureImportError("controlled JSON boolean data is invalid")
            enabled = enabled == "true"
        records.append(
            {
                "id": str(item["id"]).strip(),
                "quantity": quantity,
                "unit_price": price,
                "enabled": enabled,
            }
        )
    return records


def _controlled_next_links(body: bytes, current_url: str) -> list[str]:
    class NextLinkParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.links: list[str] = []

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            values = dict(attrs)
            if (
                tag == "a"
                and "next" in values.get("class", "").split()
                and values.get("href")
            ):
                self.links.append(urljoin(current_url, str(values["href"])))

    parser = NextLinkParser()
    try:
        parser.feed(body.decode("utf-8", errors="strict"))
        parser.close()
    except (UnicodeDecodeError, ValueError) as error:
        raise FixtureImportError("controlled pagination HTML is malformed") from error
    if len(parser.links) != len(set(parser.links)):
        raise FixtureImportError("controlled pagination repeats a continuation URL")
    return parser.links


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
    expected_records_path: Path,
    max_response_bytes: int,
    expected_media_type: str = "application/json",
    expected_path: str = "/v1/items",
    expected_paths: tuple[str, ...] | None = None,
    expected_total_response_bytes: int = 200_000,
    authorized_pagination_mode: str = "natural_end",
    authorized_max_pages: int | None = None,
    authorized_origin: str = "https://api.fixture.test",
    authorized_path_prefixes: tuple[str, ...] = (),
    expected_effective_paths: tuple[str, ...] | None = None,
    expected_redirect_counts: tuple[int, ...] | None = None,
) -> VerifiedResultBundle:
    """Validate worker output and replace worker evidence with gateway evidence."""
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
    expected = [
        _strict_json(line, "independent live expected record")
        for line in _read_regular(
            expected_records_path, maximum=MAX_RESULT_BYTES, label="expected records"
        ).splitlines()
        if line
    ]
    worker_values = [
        {key: value for key, value in record.items() if key != "_provenance"}
        for record in records
    ]
    if worker_values != expected:
        raise FixtureImportError(
            "live worker records differ from independent expectations"
        )

    worker_evidence: list[dict[str, object]] = []
    for line in files["fetch-evidence.jsonl"].splitlines():
        row = _strict_json(line, "worker-declared fetch evidence")
        if not isinstance(row, dict):
            raise FixtureImportError("worker fetch evidence must be an object")
        worker_evidence.append(row)
    raw_references = _strict_json(files["raw-references.json"], "worker raw references")
    if not isinstance(raw_references, list) or len(raw_references) != len(
        worker_evidence
    ):
        raise FixtureImportError("worker raw references do not match its fetch count")
    if not worker_evidence or len(worker_evidence) > 10:
        raise FixtureImportError("controlled live fetch count is outside its bound")
    if report.get("requests") != len(worker_evidence) or report.get("pages") != len(
        worker_evidence
    ):
        raise FixtureImportError("worker completion count differs from fetch evidence")
    if (
        expected_effective_paths is not None
        and len(expected_effective_paths) != len(worker_evidence)
    ) or (
        expected_redirect_counts is not None
        and len(expected_redirect_counts) != len(worker_evidence)
    ):
        raise FixtureImportError(
            "trusted redirect expectations differ from fetch count"
        )

    trusted_pages: list[VerifiedFixturePage] = []
    expected_body_records: dict[str, list[dict[str, object]]] = {}
    pages_by_fetch_id: dict[str, VerifiedFixturePage] = {}
    aggregate_response_bytes = 0
    permitted_paths = set(expected_paths or (expected_path,))
    for page_index, worker_row in enumerate(worker_evidence):
        fetch_id = worker_row.get("fetch_id")
        if not isinstance(fetch_id, str) or any(
            page.evidence_ref == fetch_id for page in trusted_pages
        ):
            raise FixtureImportError("worker fetch identity is missing or duplicated")
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
        if aggregate_response_bytes > expected_total_response_bytes:
            raise FixtureImportError("aggregate gateway response bytes exceed policy")
        media_type = metadata.get("media_type")
        if not isinstance(media_type, str):
            raise FixtureImportError("trusted fetch media type is invalid")
        request_path = urlsplit(str(worker_row.get("source_url", ""))).path
        effective_path_digest = metadata.get("effective_path_digest")
        actual_redirect_count = metadata.get("redirect_count")
        effective_path = (
            expected_effective_paths[page_index]
            if expected_effective_paths is not None
            and page_index < len(expected_effective_paths)
            else request_path
        )
        expected_redirect_count = (
            expected_redirect_counts[page_index]
            if expected_redirect_counts is not None
            and page_index < len(expected_redirect_counts)
            else 0
        )
        if (
            metadata.get("source_id") != source_id
            or metadata.get("source_revision_id") != source_revision_id
            or metadata.get("policy_digest") != policy_digest
            or metadata.get("release_id") != release_id
            or metadata.get("build_manifest_digest") != build_manifest_digest
            or metadata.get("status") != 200
            or media_type.split(";", 1)[0].lower() != expected_media_type
            or metadata.get("effective_origin") != "https://api.fixture.test"
            or effective_path_digest != _digest(effective_path.encode("utf-8"))
            or actual_redirect_count != expected_redirect_count
        ):
            raise FixtureImportError(
                "trusted fetch evidence differs from admitted binding"
            )
        acquired_at = _time(metadata.get("acquired_at"))
        worker_media = worker_row.get("media_type")
        if not isinstance(worker_media, str):
            raise FixtureImportError("worker media type declaration is invalid")
        mismatched_declarations = [
            label
            for label, actual, expected_value in (
                ("evidence_ref", worker_row.get("evidence_ref"), fetch_id),
                (
                    "body_digest",
                    "sha256:" + str(worker_row.get("body_sha256", "")),
                    metadata.get("body_digest"),
                ),
                (
                    "body_size",
                    worker_row.get("body_size_bytes"),
                    metadata.get("body_size"),
                ),
                ("media_type", worker_media, media_type.split(";", 1)[0].lower()),
                (
                    "body_representation",
                    worker_row.get("body_representation"),
                    metadata.get("body_representation"),
                ),
                ("acquired_at", _time(worker_row.get("acquired_at")), acquired_at),
            )
            if actual != expected_value
        ]
        if mismatched_declarations:
            raise FixtureImportError(
                "worker evidence declaration diverges from gateway fields: "
                + ",".join(mismatched_declarations)
            )
        references = [
            item
            for item in raw_references
            if isinstance(item, dict) and item.get("evidence_ref") == fetch_id
        ]
        if len(references) != 1:
            raise FixtureImportError("live fetch lacks one matching raw reference")
        raw_reference = references[0]
        url = raw_reference.get("url")
        if (
            not isinstance(url, str)
            or raw_reference.get("sha256") != hashlib.sha256(body).hexdigest()
            or worker_row.get("source_url") != url
            or urlsplit(url).scheme != "https"
            or metadata.get("request_target_digest") != _digest(url.encode("utf-8"))
            or metadata.get("request_origin") != "https://api.fixture.test"
            or request_path not in permitted_paths
            or metadata.get("request_path_digest") != _digest(request_path.encode())
        ):
            raise FixtureImportError(
                "live raw reference differs from trusted fetch identity"
            )
        page = VerifiedFixturePage(
            url,
            fetch_id,
            _digest(body),
            media_type,
            body,
            acquired_at,
            expected_redirect_count,
        )
        trusted_pages.append(page)
        pages_by_fetch_id[fetch_id] = page
        expected_body_records[fetch_id] = _controlled_record_values(
            body, expected_media_type
        )

    paths = [urlsplit(page.url).path for page in trusted_pages]
    if len(set(paths)) != len(paths) or not set(paths).issubset(permitted_paths):
        raise FixtureImportError("live pagination repeated or exceeded its path scope")
    if expected_paths is not None and paths != list(expected_paths):
        raise FixtureImportError("live pagination did not acquire the planned pages")
    if expected_paths is not None:
        _validate_pagination_completion(
            trusted_pages,
            report.get("pagination_termination"),
            authorized_mode=authorized_pagination_mode,
            authorized_max_pages=authorized_max_pages,
            authorized_origin=authorized_origin,
            authorized_path_prefixes=authorized_path_prefixes,
        )

    if sum(map(len, expected_body_records.values())) != len(records):
        raise FixtureImportError("gateway body and extracted record count differ")
    record_offsets: dict[str, int] = {fetch_id: 0 for fetch_id in pages_by_fetch_id}
    for record in records:
        provenance = record.get("_provenance")
        if not isinstance(provenance, dict):
            raise FixtureImportError("live record lacks worker provenance")
        fetch_id = provenance.get("evidence_ref")
        if not isinstance(fetch_id, str) or fetch_id not in pages_by_fetch_id:
            raise FixtureImportError("live record references unknown trusted evidence")
        page = pages_by_fetch_id[fetch_id]
        body_records = expected_body_records[fetch_id]
        offset = record_offsets[fetch_id]
        if offset >= len(body_records):
            raise FixtureImportError("live records exceed their fetch evidence")
        record_offsets[fetch_id] += 1
        if (
            provenance.get("source_id") != source_id
            or provenance.get("source_revision") != source_revision_id
            or provenance.get("source_url") != page.url
            or provenance.get("requested_url") != page.url
            or provenance.get("effective_url") != page.url
            or provenance.get("evidence_ref") != page.evidence_ref
            or provenance.get("raw_sha256") != page.digest.removeprefix("sha256:")
            or {key: value for key, value in record.items() if key != "_provenance"}
            != body_records[offset]
        ):
            raise FixtureImportError(
                "live record is not supported by trusted response bytes"
            )
    if any(
        record_offsets[fetch_id] != len(body_records)
        for fetch_id, body_records in expected_body_records.items()
    ):
        raise FixtureImportError(
            "trusted response records are missing from worker output"
        )
    total_bytes = sum(map(len, files.values()))
    return VerifiedResultBundle(
        tuple(records),
        tuple(trusted_pages),
        records_digest,
        total_bytes,
    )


def _validate_pagination_completion(
    pages: list[VerifiedFixturePage],
    reported: object,
    *,
    authorized_mode: str,
    authorized_max_pages: int | None,
    authorized_origin: str,
    authorized_path_prefixes: tuple[str, ...],
) -> None:
    """Recompute completion from trusted page bodies and the admitted plan."""
    if authorized_mode not in {"natural_end", "bounded_window"}:
        raise FixtureImportError("trusted pagination completion mode is invalid")
    if not pages:
        raise FixtureImportError("live pagination has no trusted pages")
    links_by_page = [_controlled_next_links(page.body, page.url) for page in pages]
    for index, links in enumerate(links_by_page[:-1]):
        if links != [pages[index + 1].url]:
            raise FixtureImportError("live pagination continuation chain diverged")
    final_links = links_by_page[-1]
    if authorized_mode == "natural_end":
        if reported != "natural_end" or final_links:
            raise FixtureImportError("live pagination did not naturally terminate")
        return
    if (
        reported != "planned_page_limit"
        or authorized_max_pages is None
        or len(pages) != authorized_max_pages
        or len(final_links) != 1
    ):
        raise FixtureImportError(
            "live pagination did not complete its authorized window"
        )
    continuation = urlsplit(final_links[0])
    allowed_origin = urlsplit(authorized_origin)
    if (
        continuation.scheme != allowed_origin.scheme
        or continuation.netloc != allowed_origin.netloc
        or continuation.username is not None
        or continuation.password is not None
        or not any(
            continuation.path.startswith(prefix) for prefix in authorized_path_prefixes
        )
        or final_links[0] in {page.url for page in pages}
    ):
        raise FixtureImportError(
            "planned pagination continuation exceeds authorized scope"
        )
