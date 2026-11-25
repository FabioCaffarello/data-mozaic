from __future__ import annotations

import json
import stat
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from execution_coordination.adapters.scrapy_container import (
    DockerExecution,
    ExecutorConfig,
    InvalidExecutionReportError,
    ScrapyContainerExecutor,
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
    def test_executor_rejects_root_or_non_numeric_worker_identity(self):
        binding = sample_binding()
        for worker_uid in ("0:0", "1000:0", "root:root", "1000"):
            with self.subTest(worker_uid=worker_uid), self.assertRaises(ValueError):
                ExecutorConfig(
                    {(binding.bot_id, binding.build_id): binding},
                    Path("/tmp/mozaic-test-output"),
                    worker_uid=worker_uid,
                )

    def test_default_executor_worker_identity_is_non_root(self):
        binding = sample_binding()
        config = ExecutorConfig(
            {(binding.bot_id, binding.build_id): binding}, Path("/tmp/output")
        )
        uid, gid = config.worker_uid.split(":")
        self.assertTrue(uid.isdecimal())
        self.assertTrue(gid.isdecimal())
        self.assertNotEqual(uid, "0")
        self.assertNotEqual(gid, "0")

    def test_request_mount_is_readable_by_non_root_worker_and_read_only(self):
        binding = sample_binding()
        request = ExecutionRequest(
            "execution-request/v0",
            "req-bind-permissions",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            binding,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = ScrapyContainerExecutor(
                ExecutorConfig(
                    {(binding.bot_id, binding.build_id): binding}, root / "outputs"
                )
            )

            def docker_command(argv, _deadline):
                output = (
                    "container-id-123456\n"
                    if argv[1] == "create"
                    else "[{}]"
                    if argv[1] == "inspect"
                    else ""
                )
                return CompletedProcess(argv, 0, output, "")

            with (
                patch.object(executor, "_verify_image"),
                patch.object(executor, "_verify_container"),
                patch.object(
                    executor, "_deadline_command", side_effect=docker_command
                ) as command,
            ):
                execution = executor.start(request)
            request_path = execution.attempt_dir / "execution-request.json"
            self.assertEqual(stat.S_IMODE(request_path.stat().st_mode), 0o444)
            create_args = command.call_args_list[0].args[0]
            mounts = [
                create_args[index + 1]
                for index, value in enumerate(create_args[:-1])
                if value == "--mount"
            ]
            self.assertIn(
                f"type=bind,src={request_path},dst=/run/execution-request.json,readonly",
                mounts,
            )
            self.assertEqual(
                json.loads(request_path.read_text())["request_id"],
                request.request_id,
            )

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

    def test_worker_report_rejects_unknown_contract_version(self):
        binding = sample_binding()
        request = ExecutionRequest(
            "execution-request/v0",
            "req-unknown-version",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            binding,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        forged = report_document(
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
        forged["contract_version"] = "execution-report/v99"
        with self.assertRaises(InvalidExecutionReportError):
            _validate_worker_report(forged, request)

    def test_container_report_boundary_rejects_duplicate_json_properties(self):
        binding = sample_binding()
        request = ExecutionRequest(
            "execution-request/v0",
            "req-duplicate-report",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            binding,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt_dir = root / "attempt"
            attempt_dir.mkdir()
            report_path = attempt_dir / "worker-report.json"
            report_path.write_text('{"contract_version":"v0","contract_version":"v0"}')
            executor = ScrapyContainerExecutor(
                ExecutorConfig(
                    {(binding.bot_id, binding.build_id): binding}, root / "outputs"
                )
            )
            execution = DockerExecution(
                executor=executor,
                request=request,
                execution_id=request.execution_id,
                container_id="container-id-1234",
                container_name="attempt-name",
                attempt_dir=attempt_dir,
                output_mount=attempt_dir / "out",
            )
            with (
                patch(
                    "execution_coordination.adapters.scrapy_container.subprocess.run",
                    return_value=CompletedProcess([], 0, "", ""),
                ),
                self.assertRaises(InvalidExecutionReportError),
            ):
                execution._read_worker_report()

    def test_cleanup_failure_discards_success_staging_and_fails_execution(self):
        binding = sample_binding()
        request = ExecutionRequest(
            "execution-request/v0",
            "req-cleanup-failure",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            binding,
            datetime.now(UTC) + timedelta(seconds=30),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            attempt_dir = root / "attempt"
            attempt_dir.mkdir()
            output_mount = attempt_dir / "out"
            output_mount.mkdir()
            executor = ScrapyContainerExecutor(
                ExecutorConfig(
                    {(binding.bot_id, binding.build_id): binding}, root / "outputs"
                )
            )
            execution = DockerExecution(
                executor=executor,
                request=request,
                execution_id=request.execution_id,
                container_id="container-id-1234",
                container_name="attempt-name",
                attempt_dir=attempt_dir,
                output_mount=output_mount,
            )
            successful_report = report_document(
                ExecutionReport(
                    "execution-report/v0",
                    request.request_id,
                    request.run_id,
                    request.attempt_id,
                    request.execution_id,
                    "succeeded",
                    binding,
                    10,
                    records_count=1,
                    output_bytes=10,
                    output_sha256="4" * 64,
                    output_ref="attempt-output",
                )
            )
            stage = executor._output_root / ".stage-test"

            def stage_success(_document):
                stage.mkdir()
                execution._stage_path = stage
                execution._output_sha256 = "4" * 64
                execution._output_bytes = 10
                execution._records_count = 1

            with (
                patch.object(
                    executor,
                    "_command",
                    return_value=CompletedProcess([], 0, "0\n", ""),
                ),
                patch.object(
                    execution, "_read_worker_report", return_value=successful_report
                ),
                patch.object(execution, "_collect_verified_output", stage_success),
                patch.object(
                    executor,
                    "_cleanup_container",
                    side_effect=[False, False],
                ),
                patch.object(execution, "cancel") as cancel,
            ):
                result = execution.wait(request.deadline)

            self.assertEqual(result.technical_status, "failed")
            self.assertIsNone(result.output_ref)
            self.assertFalse(stage.exists())
            self.assertFalse(attempt_dir.exists())
            cancel.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
