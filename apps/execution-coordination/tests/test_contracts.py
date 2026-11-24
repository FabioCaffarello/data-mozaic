from __future__ import annotations

import json
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from execution_coordination.adapters.scrapy_container import (
    InvalidExecutionReportError,
    _validate_worker_report,
    report_document,
    request_document,
)
from execution_coordination.domain import (
    ExecutionBinding,
    ExecutionReport,
    ExecutionRequest,
)
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[3]


def sample_binding() -> ExecutionBinding:
    image = "sha256:" + "b" * 64
    return ExecutionBinding(
        "source-a",
        "rev-1",
        "bot-" + "c" * 64,
        "d" * 64,
        "e" * 64,
        "f" * 64,
        "1" * 64,
        "registry.invalid/scrapy@" + image,
        image,
        "sha256:" + "2" * 64,
        "build-fixtures",
        "3" * 64,
    )


class ContractTests(unittest.TestCase):
    def test_request_and_report_match_closed_draft_schemas(self):
        binding = sample_binding()
        request = ExecutionRequest(
            "execution-request/v0",
            "req-1",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            binding,
            datetime.now(UTC) + timedelta(seconds=30),
            correlation={"correlation_id": "trace-123"},
        )
        report = ExecutionReport(
            "execution-report/v0",
            request.request_id,
            request.run_id,
            request.attempt_id,
            str(uuid.uuid4()),
            "succeeded",
            binding,
            42,
            1,
            120,
            "4" * 64,
            "staging-reference",
        )
        request_schema = json.loads(
            (
                ROOT
                / "contracts/execution-request/draft/v0/ExecutionRequest.schema.json"
            ).read_text()
        )
        report_schema = json.loads(
            (
                ROOT / "contracts/execution-report/draft/v0/ExecutionReport.schema.json"
            ).read_text()
        )
        Draft202012Validator(request_schema, format_checker=FormatChecker()).validate(
            request_document(request)
        )
        Draft202012Validator(report_schema, format_checker=FormatChecker()).validate(
            report_document(report)
        )

    def test_unknown_execution_mode_and_correlation_fields_fail_closed(self):
        binding = sample_binding()
        with self.assertRaises(ValueError):
            ExecutionBinding(**{**binding.__dict__, "execution_mode": "live-http"})
        with self.assertRaises(ValueError):
            ExecutionRequest(
                "execution-request/v0",
                "req",
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                binding,
                datetime.now(UTC) + timedelta(seconds=10),
                {"secret": "value"},
            )

    def test_request_schema_rejects_arbitrary_module_and_paths(self):
        binding = sample_binding()
        request = ExecutionRequest(
            "execution-request/v0",
            "req-2",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            binding,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        document = request_document(request)
        document["module_path"] = "arbitrary.code"
        schema = json.loads(
            (
                ROOT
                / "contracts/execution-request/draft/v0/ExecutionRequest.schema.json"
            ).read_text()
        )
        self.assertFalse(Draft202012Validator(schema).is_valid(document))

    def test_request_schema_enforces_runtime_correlation_allowlist(self):
        binding = sample_binding()
        request = ExecutionRequest(
            "execution-request/v0",
            "req-3",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            binding,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        document = request_document(request)
        document["correlation"]["private_data"] = "not permitted"
        schema = json.loads(
            (
                ROOT
                / "contracts/execution-request/draft/v0/ExecutionRequest.schema.json"
            ).read_text()
        )
        self.assertFalse(Draft202012Validator(schema).is_valid(document))

    def test_worker_report_rejects_partial_output_on_failure(self):
        binding = sample_binding()
        request = ExecutionRequest(
            "execution-request/v0",
            "req-4",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            binding,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        report = report_document(
            ExecutionReport(
                "execution-report/v0",
                request.request_id,
                request.run_id,
                request.attempt_id,
                request.execution_id,
                "failed",
                binding,
                1,
            )
        )
        report["counts"]["records"] = 1
        with self.assertRaises(InvalidExecutionReportError):
            _validate_worker_report(report, request)


if __name__ == "__main__":
    unittest.main()
