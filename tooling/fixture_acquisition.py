"""Trusted local import of a bounded, protected fixture-worker result bundle."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import (
    ArtifactDescription,
    ArtifactItem,
    ArtifactRef,
    CompositeOutput,
    OutputError,
    OutputIdentity,
    OutputSetBinding,
    OutputSetSpec,
    PayloadContractRef,
    ProducerIdentity,
    ScopedOperationIdentity,
    SourceRevisionRef,
    canonical_json,
    derive_output_id,
)
from acquisition_output.domain import (
    ExecutionBinding as OutputExecutionBinding,
)
from execution_coordination.adapters.scrapy_container import request_identities
from execution_coordination.domain import ExecutionReport
from jsonschema import Draft202012Validator, FormatChecker

MAX_REPORT_BYTES = 64_000
MAX_RESULT_BYTES = 16_777_216
MAX_RECORD_LINE_BYTES = 1_000_000
MAX_FIXTURE_INDEX_BYTES = 1_000_000
MAX_FIXTURE_BODY_BYTES = 2_000_000


class FixtureImportError(ValueError):
    """A worker result or fixture bundle failed the trusted import boundary."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FixtureImportError("JSON contains a duplicate property")
        result[key] = value
    return result


def _strict_json(data: bytes, label: str) -> Any:
    try:
        value = json.loads(
            data.decode("utf-8", errors="strict"), object_pairs_hook=_unique_pairs
        )
        canonical_json(value)
        return value
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        FixtureImportError,
        OutputError,
    ) as error:
        raise FixtureImportError(f"{label} is not strict UTF-8 JSON") from error


def _read_regular(path: Path, *, maximum: int, label: str) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise FixtureImportError(f"{label} is unavailable") from error
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise FixtureImportError(f"{label} is not a bounded regular file")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise FixtureImportError(f"{label} could not be read") from error
    if len(data) != info.st_size:
        raise FixtureImportError(f"{label} changed while being read")
    return data


@dataclass(frozen=True, slots=True)
class VerifiedFixturePage:
    url: str
    evidence_ref: str
    digest: str
    media_type: str
    body: bytes
    acquired_at: datetime | None = None
    redirect_count: int = 0


@dataclass(frozen=True, slots=True)
class VerifiedResultBundle:
    records: tuple[dict[str, Any], ...]
    pages: tuple[VerifiedFixturePage, ...]
    records_digest: str
    total_bytes: int


def verify_result_bundle(
    result_dir: Path,
    execution_report: ExecutionReport,
    *,
    fixture_root: Path,
    catalog_entry: Any,
    expected_records_path: Path,
    execution_report_schema_path: Path,
) -> VerifiedResultBundle:
    """Validate the trusted adapter's persisted files and immutable fixtures."""
    binding = execution_report.binding
    if (
        execution_report.technical_status != "succeeded"
        or execution_report.output_ref is None
        or Path(execution_report.output_ref) != result_dir
        or result_dir.is_symlink()
        or result_dir.name != execution_report.attempt_id
        or result_dir.parent.name != execution_report.run_id
    ):
        raise FixtureImportError(
            "result directory is not bound to the successful attempt"
        )
    if any(
        path.is_symlink()
        for path in (result_dir, result_dir.parent, result_dir.parent.parent)
    ):
        raise FixtureImportError("result bundle path contains a symlink")
    if any(
        path.is_symlink()
        for path in (
            fixture_root,
            fixture_root.parent,
            fixture_root.parent.parent,
        )
    ):
        raise FixtureImportError("immutable fixture root contains a symlink")
    try:
        entries = list(result_dir.iterdir())
    except OSError as error:
        raise FixtureImportError("result directory cannot be inspected") from error
    expected_names = {
        "records.jsonl",
        "report.json",
        "raw-references.json",
        "execution-report.json",
    }
    if {entry.name for entry in entries} != expected_names or any(
        entry.is_symlink() or not entry.is_file() for entry in entries
    ):
        raise FixtureImportError("result bundle file membership is invalid")
    file_bytes = {
        name: _read_regular(
            result_dir / name,
            maximum=MAX_REPORT_BYTES
            if name in {"report.json", "execution-report.json"}
            else MAX_RESULT_BYTES,
            label=name,
        )
        for name in expected_names
    }
    total_bytes = sum(map(len, file_bytes.values()))
    if total_bytes > min(binding.max_output_bytes, MAX_RESULT_BYTES):
        raise FixtureImportError("result bundle exceeds the execution byte budget")
    if execution_report.output_bytes != total_bytes:
        raise FixtureImportError("trusted byte count differs from the persisted bundle")

    records_bytes = file_bytes["records.jsonl"]
    records_digest = hashlib.sha256(records_bytes).hexdigest()
    if execution_report.output_sha256 != records_digest:
        raise FixtureImportError("trusted execution digest differs from records")
    lines = records_bytes.splitlines(keepends=True)
    if len(lines) != execution_report.records_count or len(lines) > binding.max_records:
        raise FixtureImportError(
            "record count differs from the trusted execution report"
        )
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.endswith(b"\n") or len(line) > MAX_RECORD_LINE_BYTES:
            raise FixtureImportError("record line is malformed or exceeds its bound")
        record = _strict_json(line[:-1], "record")
        if not isinstance(record, dict):
            raise FixtureImportError("record payload must be a JSON object")
        try:
            canonical_json(record)
        except OutputError as error:
            raise FixtureImportError(
                "record payload contains unsupported interoperable JSON values"
            ) from error
        records.append(record)

    worker_report = _strict_json(
        file_bytes["execution-report.json"], "execution report"
    )
    schema = _strict_json(
        execution_report_schema_path.read_bytes(), "execution report schema"
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(worker_report)
    identities = request_identities(binding)
    if (
        worker_report.get("contract_version") != "execution-report/v0"
        or worker_report.get("technical_status") != "succeeded"
        or worker_report.get("request_id") != execution_report.request_id
        or worker_report.get("run_id") != execution_report.run_id
        or worker_report.get("attempt_id") != execution_report.attempt_id
        or worker_report.get("execution_id") != execution_report.execution_id
        or worker_report.get("identities") != identities
        or worker_report.get("outputs", {}).get("records_sha256") != records_digest
        or not isinstance(worker_report.get("outputs", {}).get("temporary_ref"), str)
        or not worker_report.get("outputs", {}).get("temporary_ref")
        or "/" in worker_report.get("outputs", {}).get("temporary_ref", "")
        or worker_report.get("outputs", {}).get("temporary_ref", "").startswith(".")
        or worker_report.get("counts", {}).get("records") != len(records)
        or worker_report.get("counts", {}).get("output_bytes")
        != sum(
            len(file_bytes[name]) for name in expected_names - {"execution-report.json"}
        )
    ):
        raise FixtureImportError("ExecutionReport does not match the durable request")

    runtime_report = _strict_json(file_bytes["report.json"], "worker output report")
    if (
        not isinstance(runtime_report, dict)
        or runtime_report.get("state") != "passed"
        or runtime_report.get("records_file") != "records.jsonl"
        or runtime_report.get("records_sha256") != records_digest
        or runtime_report.get("records") != len(records)
    ):
        raise FixtureImportError("runtime report does not verify the records bundle")

    fixture_index_data = _read_regular(
        fixture_root / "index.json",
        maximum=MAX_FIXTURE_INDEX_BYTES,
        label="fixture index",
    )
    fixture_index = _strict_json(fixture_index_data, "fixture index")
    if (
        not isinstance(fixture_index, dict)
        or fixture_index.get("complete") is not True
        or fixture_index.get("source")
        != {"source_id": binding.source_id, "revision": binding.source_revision}
        or not isinstance(fixture_index.get("pages"), list)
        or len(fixture_index["pages"]) > 200
    ):
        raise FixtureImportError("fixture index differs from the immutable BotBuild")
    indexed_pages: dict[tuple[str, str, str], VerifiedFixturePage] = {}
    for item in fixture_index["pages"]:
        if not isinstance(item, dict):
            raise FixtureImportError("fixture page entry must be an object")
        path_value = item.get("body_file")
        if not isinstance(path_value, str) or not path_value:
            raise FixtureImportError("fixture body path is missing")
        relative = Path(path_value)
        if relative.is_absolute() or ".." in relative.parts or "\\" in path_value:
            raise FixtureImportError("fixture body path is unsafe")
        body_path = fixture_root / relative
        current = fixture_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise FixtureImportError("fixture path contains a symlink")
        body = _read_regular(
            body_path, maximum=MAX_FIXTURE_BODY_BYTES, label="fixture body"
        )
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if (
            item.get("sha256") != digest.removeprefix("sha256:")
            or item.get("size_bytes") != len(body)
            or not isinstance(item.get("url"), str)
            or not isinstance(item.get("evidence_ref"), str)
            or not isinstance(item.get("content_type"), str)
        ):
            raise FixtureImportError("fixture bytes differ from the immutable index")
        key = (item["url"], item["evidence_ref"], digest.removeprefix("sha256:"))
        if key in indexed_pages:
            raise FixtureImportError("fixture index contains duplicate page identity")
        indexed_pages[key] = VerifiedFixturePage(
            item["url"], item["evidence_ref"], digest, item["content_type"], body
        )

    declared_fixtures = {
        (item.get("url"), item.get("evidence_ref"), item.get("sha256"))
        for item in catalog_entry.input_fixtures
    }
    if set(indexed_pages) != declared_fixtures:
        raise FixtureImportError("fixture index differs from the selected BotBuild")

    raw_references = _strict_json(file_bytes["raw-references.json"], "raw references")
    if not isinstance(raw_references, list) or len(raw_references) > 200:
        raise FixtureImportError("raw references must be a bounded array")
    page_keys: set[tuple[str, str, str]] = set()
    for reference in raw_references:
        if not isinstance(reference, dict) or set(reference) != {
            "url",
            "evidence_ref",
            "sha256",
        }:
            raise FixtureImportError("raw reference fields are invalid")
        key = (reference["url"], reference["evidence_ref"], reference["sha256"])
        if key not in declared_fixtures or key in page_keys:
            raise FixtureImportError("raw reference is unknown or duplicated")
        page_keys.add(key)

    observed_page_keys: set[tuple[str, str, str]] = set()
    for record in records:
        provenance = record.get("_provenance")
        if not isinstance(provenance, dict):
            raise FixtureImportError("record lacks trusted fixture provenance")
        key = (
            provenance.get("source_url"),
            provenance.get("evidence_ref"),
            provenance.get("raw_sha256"),
        )
        if (
            provenance.get("source_id") != binding.source_id
            or provenance.get("source_revision") != binding.source_revision
            or provenance.get("requested_url") != provenance.get("source_url")
            or provenance.get("effective_url") != provenance.get("source_url")
            or key not in declared_fixtures
            or key not in page_keys
            or not isinstance(provenance.get("page_index"), int)
            or isinstance(provenance.get("page_index"), bool)
            or provenance["page_index"] < 0
            or not isinstance(provenance.get("record_index"), int)
            or isinstance(provenance.get("record_index"), bool)
            or provenance["record_index"] < 0
        ):
            raise FixtureImportError(
                "record provenance differs from the selected fixture"
            )
        observed_page_keys.add(key)
    if not observed_page_keys.issubset(page_keys):
        raise FixtureImportError("records refer to a page omitted from raw references")

    expected_bytes = _read_regular(
        expected_records_path,
        maximum=MAX_RESULT_BYTES,
        label="independent expected records",
    )
    expected_records = [
        _strict_json(line, "expected record")
        for line in expected_bytes.splitlines()
        if line
    ]
    if records != expected_records:
        raise FixtureImportError("worker records differ from independent expectations")

    relevant_pages = tuple(indexed_pages[key] for key in sorted(observed_page_keys))
    return VerifiedResultBundle(
        tuple(records), relevant_pages, records_digest, total_bytes
    )


class FixturePayloadContractValidator:
    def __init__(self, contract: PayloadContractRef, schema_path: Path) -> None:
        schema_data = _read_regular(schema_path, maximum=64_000, label="payload schema")
        self._contract = contract
        self._validator = Draft202012Validator(
            _strict_json(schema_data, "payload schema"), format_checker=FormatChecker()
        )

    def validate(self, contract: PayloadContractRef, payload: dict[str, Any]) -> None:
        if contract != self._contract:
            raise FixtureImportError(
                "payload contract differs from the trusted fixture"
            )
        self._validator.validate(payload)


def _stable_id(prefix: str, identity: object) -> str:
    return prefix + hashlib.sha256(canonical_json(identity)).hexdigest()


def _custody_request(
    base_url: str,
    token: str,
    tenant_id: str,
    path: str,
    *,
    method: str,
    body: bytes,
):
    request = Request(
        base_url.rstrip("/") + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "X-DM-Tenant-ID": tenant_id,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return _strict_json(response.read(64_000), "BC06 response")
    except (HTTPError, URLError, TimeoutError, OSError, FixtureImportError) as error:
        raise FixtureImportError("BC06 custody request failed") from error


def import_verified_bundle(
    application: AcquisitionOutputApplication,
    *,
    bundle: VerifiedResultBundle,
    tenant_id: str,
    source: SourceRevisionRef,
    producer: ProducerIdentity,
    run_id: str,
    attempt_id: str,
    fencing_generation: int,
    observed_at: datetime,
    payload_contract: PayloadContractRef,
    custody_base_url: str,
    custody_token: str,
    artifact_representation: str = "synthetic_fixture_http_response_body",
    retention_policy_ref: str = "local-fixture-v1",
    artifact_title: str = "Synthetic fixture HTTP entity",
    artifact_role: str = "fixture-http-entity",
) -> tuple[str, str]:
    """Custody verified response entities, stage deterministic outputs, then seal."""
    references: dict[tuple[str, str, str], ArtifactRef] = {}
    for page in bundle.pages:
        artifact_id = _stable_id(
            "artifact-",
            {
                "tenant": tenant_id,
                "run": run_id,
                "attempt": attempt_id,
                "url": page.url,
                "page": page.evidence_ref,
                "digest": page.digest,
            },
        )
        operation_id = _stable_id(
            "op-",
            {
                "purpose": "fixture-custody",
                "tenant": tenant_id,
                "run": run_id,
                "attempt": attempt_id,
                "artifact": artifact_id,
            },
        )
        artifact = ArtifactRef(
            artifact_id, page.digest, len(page.body), page.media_type
        )
        intent = {
            "contract_version": "artifact-custody.v1",
            "operation_id": operation_id,
            "tenant_id": tenant_id,
            "artifact_id": artifact_id,
            "source": {"source_id": source.source_id, "revision": source.revision},
            "artifact": {
                "artifact_id": artifact_id,
                "digest": artifact.digest,
                "size_bytes": artifact.size_bytes,
                "media_type": artifact.media_type,
            },
            "representation": "other_declared",
            "classification": "internal",
            "retention_policy_ref": retention_policy_ref,
            "metadata": {
                "declared_representation": artifact_representation,
                "evidence_ref": page.evidence_ref,
            },
        }
        payload = json.dumps(intent, ensure_ascii=False, separators=(",", ":")).encode()
        reservation = _custody_request(
            custody_base_url,
            custody_token,
            tenant_id,
            "/draft/v1/artifact-intents",
            method="POST",
            body=payload,
        )
        if reservation.get("artifact_id") != artifact_id:
            raise FixtureImportError("BC06 reserved a different artifact identity")
        receipt = reservation.get("receipt")
        if not isinstance(receipt, dict):
            receipt = _custody_request(
                custody_base_url,
                custody_token,
                tenant_id,
                f"/draft/v1/artifact-intents/{operation_id}/content",
                method="PUT",
                body=page.body,
            )
        query = (
            "/draft/v1/artifacts/"
            + artifact_id
            + "/verification?"
            + urlencode(
                {
                    "digest": artifact.digest,
                    "size_bytes": artifact.size_bytes,
                    "source_id": source.source_id,
                    "source_revision": source.revision,
                }
            )
        )
        verification = _custody_request(
            custody_base_url,
            custody_token,
            tenant_id,
            query,
            method="GET",
            body=b"",
        )
        receipt = verification.get("receipt")
        if (
            verification.get("status") != "confirmed"
            or not isinstance(receipt, dict)
            or receipt.get("tenant_id") != tenant_id
            or receipt.get("artifact", {}).get("artifact_id") != artifact_id
            or receipt.get("artifact", {}).get("digest") != artifact.digest
            or receipt.get("artifact", {}).get("size_bytes") != artifact.size_bytes
        ):
            raise FixtureImportError("BC06 did not confirm the exact fixture bytes")
        references[
            (page.url, page.evidence_ref, page.digest.removeprefix("sha256:"))
        ] = artifact

    output_binding = OutputSetBinding(
        tenant_id,
        source,
        producer,
        OutputExecutionBinding(run_id, attempt_id, fencing_generation),
    )
    output_set_id = _stable_id(
        "set-",
        {
            "tenant": tenant_id,
            "run": run_id,
            "attempt": attempt_id,
            "records": bundle.records_digest,
        },
    )
    application.create_output_set(
        OutputSetSpec(
            output_set_id,
            output_binding,
            (payload_contract,),
            expected_output_count=len(bundle.records),
        ),
        ScopedOperationIdentity(
            tenant_id,
            _stable_id("op-", {"purpose": "create-set", "output_set": output_set_id}),
        ),
    )
    for record in bundle.records:
        provenance = record["_provenance"]
        page_key = (
            provenance["source_url"],
            provenance["evidence_ref"],
            provenance["raw_sha256"],
        )
        artifact = references.get(page_key)
        if artifact is None:
            raise FixtureImportError("record has no custody-confirmed fixture page")
        observation_id = _stable_id(
            "obs-",
            {
                "run": run_id,
                "attempt": attempt_id,
                "page_index": provenance["page_index"],
                "record_index": provenance["record_index"],
                "records_digest": bundle.records_digest,
            },
        )
        output_id = derive_output_id(
            tenant_id=tenant_id,
            source=source,
            producer=producer,
            output_key=f"fixture-record:{provenance['page_index']}:{provenance['record_index']}",
            observation_id=observation_id,
        )
        output = CompositeOutput(
            identity=OutputIdentity(
                output_id,
                observation_id,
                next(
                    (
                        page.acquired_at
                        for page in bundle.pages
                        if page.url == provenance["source_url"]
                        and page.evidence_ref == provenance["evidence_ref"]
                        and page.digest.removeprefix("sha256:")
                        == provenance["raw_sha256"]
                        and page.acquired_at is not None
                    ),
                    observed_at,
                ),
                natural_key=record.get("id"),
            ),
            operation=ScopedOperationIdentity(
                tenant_id,
                _stable_id("op-", {"purpose": "stage-output", "output_id": output_id}),
            ),
            output_set_id=output_set_id,
            binding=output_binding,
            payload_contract=payload_contract,
            payload=record,
            artifacts=(
                ArtifactItem(
                    artifact,
                    ArtifactDescription(
                        title=artifact_title,
                        role=artifact_role,
                    ),
                ),
            ),
        )
        application.stage_output(output)
    sealed = application.seal_output_set(
        tenant_id,
        output_set_id,
        ScopedOperationIdentity(
            tenant_id,
            _stable_id("op-", {"purpose": "seal-set", "output_set": output_set_id}),
        ),
    )
    return output_set_id, sealed.manifest_digest
