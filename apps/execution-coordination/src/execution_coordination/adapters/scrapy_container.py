"""BC05 technical adapter invoking only a prebuilt, trusted Scrapy image."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from execution_coordination.domain import (
    ExecutionBinding,
    ExecutionReport,
    ExecutionRequest,
)
from execution_coordination.ports import ExecutionPortError


class ProtectedExecutionError(ExecutionPortError):
    """The trusted image or effective container configuration failed validation."""


class InvalidExecutionReportError(ProtectedExecutionError):
    """The worker report or output failed the closed response contract."""


@dataclass(frozen=True)
class SandboxLimits:
    cpus: float = 1.0
    memory_bytes: int = 536_870_912
    pids: int = 64
    stop_grace_seconds: int = 2
    command_timeout_seconds: int = 15

    def __post_init__(self) -> None:
        if not 0 < self.cpus <= 4:
            raise ValueError("sandbox CPU limit must be within (0, 4]")
        if not 0 < self.memory_bytes <= 2_147_483_648:
            raise ValueError("sandbox memory limit must be within (0, 2 GiB]")
        if not 0 < self.pids <= 256:
            raise ValueError("sandbox PID limit must be within (0, 256]")
        if not 0 <= self.stop_grace_seconds <= 10:
            raise ValueError("sandbox stop grace must be within [0, 10]")
        if not 1 <= self.command_timeout_seconds <= 60:
            raise ValueError("Docker command timeout must be within [1, 60]")


@dataclass(frozen=True)
class ExecutorConfig:
    trusted_bindings: dict[tuple[str, str], ExecutionBinding]
    output_root: Path
    limits: SandboxLimits = SandboxLimits()
    worker_uid: str = "65532:65532"


class ScrapyContainerExecutor:
    """Implements the BC04 ExecutionPort using the local Docker CLI as trusted adapter."""

    def __init__(self, config: ExecutorConfig) -> None:
        self.config = config
        root = config.output_root.absolute()
        if root.is_symlink():
            raise ProtectedExecutionError("output root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir():
            raise ProtectedExecutionError("output root must be a directory")
        self._output_root = root

    def publish(self, report: ExecutionReport) -> ExecutionReport:
        if report.technical_status != "succeeded" or not report.output_ref:
            raise ProtectedExecutionError(
                "only verified successful output can be published"
            )
        stage = Path(report.output_ref)
        stage_prefix = f".stage-{report.run_id}-{report.attempt_id}-"
        if (
            stage.parent != self._output_root
            or not stage.name.startswith(stage_prefix)
            or stage.is_symlink()
            or not stage.is_dir()
        ):
            raise ProtectedExecutionError(
                "output staging reference is outside executor ownership"
            )
        final_parent = self._output_root / report.run_id
        if final_parent.is_symlink():
            raise ProtectedExecutionError("Run output directory cannot be a symlink")
        final_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        final = final_parent / report.attempt_id
        if final.exists() or final.is_symlink():
            raise ProtectedExecutionError("Attempt output already exists")
        os.replace(stage, final)
        return dataclass_replace(report, output_ref=str(final))

    def discard(self, report: ExecutionReport) -> None:
        if report.output_ref:
            stage = Path(report.output_ref)
            if stage.parent == self._output_root and stage.name.startswith(".stage-"):
                shutil.rmtree(stage, ignore_errors=True)

    def start(self, request: ExecutionRequest) -> DockerExecution:
        trusted = self.config.trusted_bindings.get(
            (request.binding.bot_id, request.binding.build_id)
        )
        if trusted is None or request.binding != trusted:
            raise ProtectedExecutionError(
                "request identity is not in trusted executor configuration"
            )
        remaining = (request.deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("deadline elapsed before container creation")
        self._verify_image(trusted, request.deadline)
        execution_id = request.execution_id
        container_name = f"mozaic-attempt-{request.attempt_id.replace('-', '')[:24]}"
        attempt_dir = (
            self._output_root
            / f".{request.run_id}.{request.attempt_id}.{uuid.uuid4().hex}"
        )
        attempt_dir.mkdir(mode=0o700)
        output_mount = attempt_dir / "out"
        output_mount.mkdir(mode=0o777)
        os.chmod(output_mount, 0o777)
        request_path = attempt_dir / "execution-request.json"
        request_path.write_text(
            json.dumps(
                request_document(request), sort_keys=True, separators=(",", ":")
            ),
            encoding="utf-8",
        )
        # The worker runs as UID 65532, which differs from the host runner UID on
        # Linux. The bind itself is read-only, so grant read access without write
        # access; the parent attempt directory remains private on the host.
        request_path.chmod(0o444)
        args = [
            "docker",
            "create",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--user",
            self.config.worker_uid,
            "--cpus",
            str(self.config.limits.cpus),
            "--memory",
            str(self.config.limits.memory_bytes),
            "--memory-swap",
            str(self.config.limits.memory_bytes),
            "--pids-limit",
            str(self.config.limits.pids),
            "--ulimit",
            "fsize=20000000:20000000",
            "--security-opt",
            "no-new-privileges",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=2",
            "--cap-drop",
            "ALL",
            "--mount",
            f"type=bind,src={output_mount},dst=/out",
            "--mount",
            f"type=bind,src={request_path},dst=/run/execution-request.json,readonly",
            "--env",
            "SCRAPY_COLLECTOR_OUTPUT_ROOT=/out",
            trusted.local_image_id,
            "run",
            "--request-file",
            "/run/execution-request.json",
        ]
        container_id: str | None = None
        try:
            container_id = self._deadline_command(args, request.deadline).stdout.strip()
            if not container_id or len(container_id) < 12:
                raise ProtectedExecutionError(
                    "Docker did not return a container identity"
                )
            container = json.loads(
                self._deadline_command(
                    ["docker", "inspect", container_id], request.deadline
                ).stdout
            )[0]
            self._verify_container(container, trusted, output_mount, request_path)
            self._deadline_command(["docker", "start", container_id], request.deadline)
            return DockerExecution(
                executor=self,
                request=request,
                execution_id=execution_id,
                container_id=container_id,
                container_name=container_name,
                attempt_dir=attempt_dir,
                output_mount=output_mount,
            )
        except (
            ProtectedExecutionError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            subprocess.TimeoutExpired,
        ):
            try:
                self._cleanup_container(container_id or container_name)
            finally:
                shutil.rmtree(attempt_dir, ignore_errors=True)
            raise

    def _verify_image(self, binding: ExecutionBinding, deadline: datetime) -> None:
        if "@" not in binding.oci_image or not binding.oci_digest.startswith("sha256:"):
            raise ProtectedExecutionError(
                "trusted image must use an immutable OCI digest reference"
            )
        if not binding.oci_image.endswith("@" + binding.oci_digest):
            raise ProtectedExecutionError("OCI digest and image reference differ")
        info = json.loads(
            self._deadline_command(
                ["docker", "image", "inspect", binding.local_image_id], deadline
            ).stdout
        )[0]
        if info.get("Id") != binding.local_image_id:
            raise ProtectedExecutionError(
                "local image ID differs from trusted configuration"
            )
        if info.get("Os") != "linux" or info.get("Architecture") != "amd64":
            raise ProtectedExecutionError("worker image must target linux/amd64")
        repo_digests = info.get("RepoDigests") or []
        if repo_digests and binding.oci_image not in repo_digests:
            raise ProtectedExecutionError(
                "image OCI repository digest differs from trusted configuration"
            )
        # A locally loaded CI image may lack RepoDigests. The daemon still executes by
        # immutable image ID; the OCI manifest digest remains a separate trusted build identity.
        labels = (info.get("Config") or {}).get("Labels") or {}
        if (
            labels.get("org.data-mozaic.scrapy-collector.assembly-id")
            != binding.assembly_id
        ):
            raise ProtectedExecutionError(
                "image assembly label differs from trusted binding"
            )
        if (info.get("Config") or {}).get("Entrypoint") != [
            "python",
            "-B",
            "-m",
            "scrapy_collector",
        ]:
            raise ProtectedExecutionError(
                "worker image entrypoint is not the approved collector"
            )

    def _verify_container(
        self,
        container: dict[str, Any],
        binding: ExecutionBinding,
        output_mount: Path,
        request_path: Path,
    ) -> None:
        config = container.get("Config") or {}
        host = container.get("HostConfig") or {}
        mounts = container.get("Mounts") or []
        mount_map = {item.get("Destination"): item for item in mounts}
        output = mount_map.get("/out")
        request = mount_map.get("/run/execution-request.json")
        expected_cpus = int(self.config.limits.cpus * 1_000_000_000)
        checks = {
            "image_id": container.get("Image") == binding.local_image_id,
            "non_root_user": config.get("User") == self.config.worker_uid,
            "entrypoint": config.get("Entrypoint")
            == ["python", "-B", "-m", "scrapy_collector"],
            "network_none": host.get("NetworkMode") == "none",
            "readonly_root": host.get("ReadonlyRootfs") is True,
            "cpu_limit": host.get("NanoCpus") == expected_cpus,
            "memory_limit": host.get("Memory") == self.config.limits.memory_bytes,
            "memory_and_swap_limit": host.get("MemorySwap")
            == self.config.limits.memory_bytes,
            "pid_limit": host.get("PidsLimit") == self.config.limits.pids,
            "capabilities_dropped": host.get("CapDrop") == ["ALL"],
            "not_privileged": host.get("Privileged") is False,
            "no_devices": not host.get("Devices") and not host.get("DeviceRequests"),
            "bounded_file_size": any(
                isinstance(item, dict)
                and item.get("Name") == "fsize"
                and item.get("Soft") == 20_000_000
                and item.get("Hard") == 20_000_000
                for item in host.get("Ulimits") or []
            ),
            "request_mount": bool(
                request
                and request.get("Source") == str(request_path)
                and request.get("RW") is False
            ),
            "output_mount": bool(
                output
                and output.get("Type") == "bind"
                and output.get("Source") == str(output_mount)
                and output.get("RW") is True
            ),
            "mount_set": set(mount_map) == {"/out", "/run/execution-request.json"},
            "environment": config.get("Env") is not None,
            "no_python_path_override": not any(
                item.startswith(("PYTHONPATH=", "PYTHONHOME="))
                for item in config.get("Env") or []
            ),
            "no_secrets": not any(
                any(
                    secret in item.split("=", 1)[0].upper()
                    for secret in ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
                )
                for item in config.get("Env") or []
            ),
            "fixed_command": config.get("Cmd")
            == ["run", "--request-file", "/run/execution-request.json"],
            "no_new_privileges": host.get("SecurityOpt")
            in (["no-new-privileges:true"], ["no-new-privileges"]),
            "bounded_container_logs": (host.get("LogConfig") or {}).get("Type")
            == "local"
            and (host.get("LogConfig") or {}).get("Config")
            == {"max-size": "1m", "max-file": "2"},
            "no_extra_binds": host.get("Binds") in (None, []),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise ProtectedExecutionError(
                "effective container configuration violates sandbox policy: "
                + ",".join(failed)
            )

    def _deadline_command(
        self, argv: list[str], deadline: datetime
    ) -> subprocess.CompletedProcess[str]:
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            raise TimeoutError("Attempt deadline elapsed during container startup")
        return self._command(
            argv, timeout=min(remaining, self.config.limits.command_timeout_seconds)
        )

    def _command(
        self, argv: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._docker_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProtectedExecutionError(
                f"trusted Docker operation failed: {type(error).__name__}"
            ) from error
        if result.returncode:
            raise ProtectedExecutionError(
                f"Docker operation failed ({result.returncode}): {result.stderr[-2000:]}"
            )
        return result

    @staticmethod
    def _docker_environment() -> dict[str, str]:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG", ""),
        }

    def _best_effort_command(self, argv: list[str], *, timeout: float) -> None:
        try:
            subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._docker_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _cleanup_container(self, container_id: str) -> bool:
        try:
            result = subprocess.run(
                ["docker", "rm", "--force", container_id],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.limits.command_timeout_seconds,
                env=self._docker_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 or "no such container" in result.stderr.lower()


class DockerExecution:
    def __init__(
        self,
        *,
        executor: ScrapyContainerExecutor,
        request: ExecutionRequest,
        execution_id: str,
        container_id: str,
        container_name: str,
        attempt_dir: Path,
        output_mount: Path,
    ) -> None:
        self.executor = executor
        self.request = request
        self.execution_id = execution_id
        self.container_id = container_id
        self.container_name = container_name
        self.attempt_dir = attempt_dir
        self.output_mount = output_mount
        self._cancelled = threading.Event()
        self._wait_lock = threading.Lock()
        self._started_at = time.monotonic()
        self._finished = False

    def cancel(self) -> None:
        self._cancelled.set()
        self.executor._best_effort_command(
            [
                "docker",
                "stop",
                "--time",
                str(self.executor.config.limits.stop_grace_seconds),
                self.container_id,
            ],
            timeout=self.executor.config.limits.stop_grace_seconds
            + self.executor.config.limits.command_timeout_seconds,
        )
        try:
            inspected = self.executor._command(
                ["docker", "inspect", self.container_id],
                timeout=self.executor.config.limits.command_timeout_seconds,
            )
            state = json.loads(inspected.stdout)[0].get("State", {}).get("Status")
            if state not in {"exited", "dead"}:
                self.executor._best_effort_command(
                    ["docker", "kill", self.container_id],
                    timeout=self.executor.config.limits.command_timeout_seconds,
                )
        except (ProtectedExecutionError, json.JSONDecodeError, IndexError, TypeError):
            pass

    def wait(self, deadline: datetime) -> ExecutionReport:
        with self._wait_lock:
            remaining = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
            status = "crashed"
            diagnostic = "container exited without a valid worker result"
            try:
                try:
                    wait_result = self.executor._command(
                        ["docker", "wait", self.container_id],
                        timeout=min(
                            remaining,
                            self.executor.config.limits.command_timeout_seconds
                            + remaining,
                        ),
                    )
                except ProtectedExecutionError as error:
                    if (
                        "timed out" in str(error).lower()
                        or datetime.now(UTC) >= deadline
                    ):
                        self.cancel()
                        status, diagnostic = "timed_out", "container deadline exceeded"
                        wait_result = None
                    else:
                        raise
                if wait_result is not None:
                    exit_code = int(wait_result.stdout.strip())
                    result_doc = self._read_worker_report()
                    result_doc = _validate_worker_report(result_doc, self.request)
                    if self._cancelled.is_set():
                        status, diagnostic = (
                            "cancelled",
                            "container cancellation requested",
                        )
                    elif (
                        result_doc
                        and result_doc.get("technical_status") == "succeeded"
                        and exit_code == 0
                    ):
                        status, diagnostic = "succeeded", ""
                    elif result_doc and result_doc.get("technical_status") in {
                        "timed_out",
                        "cancelled",
                        "failed",
                        "crashed",
                    }:
                        status = result_doc["technical_status"]
                        diagnostic = str(
                            result_doc.get("diagnostics", [{}])[0].get(
                                "message", status
                            )
                        )
                    elif exit_code != 0:
                        inspect = json.loads(
                            self.executor._command(
                                ["docker", "inspect", self.container_id],
                                timeout=self.executor.config.limits.command_timeout_seconds,
                            ).stdout
                        )[0]
                        state_info = inspect.get("State") or {}
                        status = (
                            "crashed"
                            if state_info.get("OOMKilled")
                            or exit_code not in {0, 1, 2, 70}
                            else "failed"
                        )
                        logs = self._bounded_container_logs()
                        diagnostic = f"container exited without a valid report (exit {exit_code})"
                        if logs:
                            diagnostic += f"; bounded container output: {logs}"
                    else:
                        status, diagnostic = (
                            "failed",
                            "worker did not report a successful terminal result",
                        )
                    if status == "succeeded":
                        self._collect_verified_output(result_doc)
            except InvalidExecutionReportError as error:
                status, diagnostic = "failed", str(error)
            except (
                ProtectedExecutionError,
                OSError,
                ValueError,
                TypeError,
                subprocess.TimeoutExpired,
                json.JSONDecodeError,
                IndexError,
            ) as error:
                status, diagnostic = (
                    "crashed",
                    f"container supervision failed: {type(error).__name__}",
                )
            finally:
                container_clean = self.executor._cleanup_container(self.container_id)
                if not container_clean:
                    self.cancel()
                    container_clean = self.executor._cleanup_container(
                        self.container_id
                    )
                self._finished = True
                shutil.rmtree(self.attempt_dir, ignore_errors=True)
                if not container_clean:
                    if status == "succeeded":
                        status = "failed"
                    diagnostic = (diagnostic + "; " if diagnostic else "") + (
                        "container cleanup failed"
                    )
                if status != "succeeded":
                    staged = getattr(self, "_stage_path", None)
                    if staged:
                        shutil.rmtree(staged, ignore_errors=True)
                    self._stage_path = None
                    self._output_sha256 = None
                    self._output_bytes = 0
                    self._records_count = 0
            return ExecutionReport(
                contract_version="execution-report/v0",
                request_id=self.request.request_id,
                run_id=self.request.run_id,
                attempt_id=self.request.attempt_id,
                execution_id=self.execution_id,
                technical_status=status,
                binding=self.request.binding,
                duration_ms=max(0, int((time.monotonic() - self._started_at) * 1000)),
                diagnostics=()
                if not diagnostic
                else ({"code": status.upper(), "message": diagnostic[:1000]},),
                output_ref=str(getattr(self, "_stage_path", ""))
                if status == "succeeded"
                else None,
                output_sha256=getattr(self, "_output_sha256", None),
                output_bytes=getattr(self, "_output_bytes", 0),
                records_count=getattr(self, "_records_count", 0),
            )

    def _read_worker_report(self) -> dict[str, Any] | None:
        report_path = self.attempt_dir / "worker-report.json"
        copied = subprocess.run(
            [
                "docker",
                "cp",
                f"{self.container_id}:/out/execution-report.json",
                str(report_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.executor.config.limits.command_timeout_seconds,
            env=self.executor._docker_environment(),
        )
        if copied.returncode:
            return None
        if (
            report_path.is_symlink()
            or not report_path.is_file()
            or report_path.stat().st_size > 64_000
        ):
            raise InvalidExecutionReportError(
                "worker report file is unsafe or exceeds its limit"
            )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidExecutionReportError(
                "worker report file is invalid JSON"
            ) from error
        return report if isinstance(report, dict) else None

    def _bounded_container_logs(self) -> str:
        result = subprocess.run(
            ["docker", "logs", "--tail", "32", self.container_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=self.executor.config.limits.command_timeout_seconds,
            env=self.executor._docker_environment(),
        )
        if result.returncode:
            return ""
        return (result.stdout + result.stderr).strip()[-600:]

    def _collect_verified_output(self, worker_result: dict[str, Any]) -> None:
        worker_id = worker_result.get("outputs", {}).get("temporary_ref")
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or "/" in worker_id
            or worker_id.startswith(".")
        ):
            raise InvalidExecutionReportError(
                "worker returned an invalid output identity"
            )
        source = self.attempt_dir / "copied-output"
        self.executor._command(
            ["docker", "cp", f"{self.container_id}:/out/{worker_id}", str(source)],
            timeout=self.executor.config.limits.command_timeout_seconds,
        )
        if source.is_symlink() or not source.is_dir():
            raise InvalidExecutionReportError("successful worker output is absent")
        files = list(source.iterdir())
        if any(item.is_symlink() or not item.is_file() for item in files):
            raise InvalidExecutionReportError("worker output contains unsafe entries")
        allowed = {"records.jsonl", "report.json", "raw-references.json"}
        if not {"records.jsonl", "report.json"}.issubset(
            {item.name for item in files}
        ) or any(item.name not in allowed for item in files):
            raise InvalidExecutionReportError("worker output file set is invalid")
        total_bytes = sum(item.stat().st_size for item in files)
        if total_bytes > self.request.binding.max_output_bytes:
            raise InvalidExecutionReportError("worker output exceeds the byte budget")
        records_path = source / "records.jsonl"
        report_path = source / "report.json"
        if records_path.stat().st_size > self.request.binding.max_output_bytes:
            raise InvalidExecutionReportError("records output exceeds the byte budget")
        records = records_path.read_bytes()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            len(records.splitlines()) > self.request.binding.max_records
            or report.get("state") != "passed"
            or report.get("records_sha256") != hashlib.sha256(records).hexdigest()
            or report.get("records") != len(records.splitlines())
            or worker_result.get("identities", {}).get("bot_id")
            != self.request.binding.bot_id
            or worker_result.get("identities", {}).get("build_id")
            != self.request.binding.build_id
            or worker_result.get("outputs", {}).get("records_sha256")
            != hashlib.sha256(records).hexdigest()
            or worker_result.get("counts", {}).get("records")
            != len(records.splitlines())
        ):
            raise InvalidExecutionReportError(
                "output integrity or identity check failed"
            )
        stage = (
            self.executor._output_root
            / f".stage-{self.request.run_id}-{self.request.attempt_id}-{uuid.uuid4().hex}"
        )
        os.replace(source, stage)
        self._stage_path = stage
        self._output_sha256 = hashlib.sha256(records).hexdigest()
        self._output_bytes = total_bytes
        self._records_count = len(records.splitlines())


def request_document(request: ExecutionRequest) -> dict[str, Any]:
    binding = request.binding
    return {
        "contract_version": request.contract_version,
        "request_id": request.request_id,
        "run_id": request.run_id,
        "attempt_id": request.attempt_id,
        "execution_id": request.execution_id,
        "source": {
            "source_id": binding.source_id,
            "source_revision": binding.source_revision,
        },
        "bot": {
            "bot_id": binding.bot_id,
            "build_id": binding.build_id,
            "manifest_digest": binding.bot_build_manifest_digest,
        },
        "assembly": {
            "assembly_id": binding.assembly_id,
            "catalog_digest": binding.catalog_digest,
        },
        "worker_image": {
            "oci_image": binding.oci_image,
            "oci_digest": binding.oci_digest,
            "local_image_id": binding.local_image_id,
        },
        "fixtures": {
            "bundle_id": binding.fixture_bundle_id,
            "bundle_digest": binding.fixture_bundle_digest,
        },
        "execution_mode": binding.execution_mode,
        "deadline": request.deadline.astimezone(UTC).isoformat(),
        "budgets": {
            "duration_seconds": binding.max_duration_seconds,
            "output_bytes": binding.max_output_bytes,
            "records": binding.max_records,
        },
        "correlation": request.correlation,
    }


def _last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (
            value.get("contract_version") == "execution-report/v0" or "state" in value
        ):
            return value
    return None


def report_document(report: ExecutionReport) -> dict[str, Any]:
    binding = report.binding
    return {
        "contract_version": report.contract_version,
        "request_id": report.request_id,
        "run_id": report.run_id,
        "attempt_id": report.attempt_id,
        "execution_id": report.execution_id,
        "identities": {
            "source_id": binding.source_id,
            "source_revision": binding.source_revision,
            "bot_id": binding.bot_id,
            "build_id": binding.build_id,
            "bot_build_manifest_digest": binding.bot_build_manifest_digest,
            "assembly_id": binding.assembly_id,
            "catalog_digest": binding.catalog_digest,
            "oci_digest": binding.oci_digest,
            "local_image_id": binding.local_image_id,
            "fixture_bundle_id": binding.fixture_bundle_id,
            "fixture_bundle_digest": binding.fixture_bundle_digest,
        },
        "technical_status": report.technical_status,
        "duration_ms": report.duration_ms,
        "counts": {
            "records": report.records_count,
            "output_bytes": report.output_bytes,
        },
        "diagnostics": list(report.diagnostics),
        "outputs": {
            "records_sha256": report.output_sha256,
            "temporary_ref": report.output_ref,
        },
    }


def _validate_worker_report(
    report: dict[str, Any] | None, request: ExecutionRequest
) -> dict[str, Any] | None:
    if report is None:
        return report
    if not isinstance(report, dict):
        raise InvalidExecutionReportError("ExecutionReport must be an object")
    if report.get("contract_version") != "execution-report/v0":
        raise InvalidExecutionReportError("ExecutionReport version is unsupported")
    if set(report) != {
        "contract_version",
        "request_id",
        "run_id",
        "attempt_id",
        "execution_id",
        "identities",
        "technical_status",
        "duration_ms",
        "counts",
        "diagnostics",
        "outputs",
    }:
        raise InvalidExecutionReportError("ExecutionReport fields are not closed")
    if report.get("technical_status") not in {
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "crashed",
    }:
        raise InvalidExecutionReportError("ExecutionReport status is unknown")
    if (
        not isinstance(report.get("duration_ms"), int)
        or isinstance(report["duration_ms"], bool)
        or report["duration_ms"] < 0
    ):
        raise InvalidExecutionReportError("ExecutionReport duration is invalid")
    if not isinstance(report.get("counts"), dict) or set(report["counts"]) != {
        "records",
        "output_bytes",
    }:
        raise InvalidExecutionReportError("ExecutionReport counts are malformed")
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in report["counts"].values()
    ):
        raise InvalidExecutionReportError("ExecutionReport counters are invalid")
    if (
        not isinstance(report.get("diagnostics"), list)
        or len(report["diagnostics"]) > 64
        or any(
            not isinstance(item, dict)
            or set(item) != {"code", "message"}
            or not isinstance(item["code"], str)
            or len(item["code"]) > 64
            or not isinstance(item["message"], str)
            or len(item["message"]) > 1000
            for item in report["diagnostics"]
        )
    ):
        raise InvalidExecutionReportError("ExecutionReport diagnostics are malformed")
    if not isinstance(report.get("outputs"), dict) or set(report["outputs"]) != {
        "records_sha256",
        "temporary_ref",
    }:
        raise InvalidExecutionReportError("ExecutionReport outputs are malformed")
    expected_ids = {
        "request_id": request.request_id,
        "run_id": request.run_id,
        "attempt_id": request.attempt_id,
        "execution_id": request.execution_id,
    }
    if any(report.get(key) != value for key, value in expected_ids.items()):
        raise InvalidExecutionReportError(
            "ExecutionReport identity does not match request"
        )
    if report.get("identities") != request_identities(request.binding):
        raise InvalidExecutionReportError(
            "ExecutionReport selected identities differ from trusted request"
        )
    counts = report["counts"]
    outputs = report["outputs"]
    if report["technical_status"] == "succeeded":
        if (
            not isinstance(outputs["temporary_ref"], str)
            or not outputs["temporary_ref"]
            or not isinstance(outputs["records_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", outputs["records_sha256"])
        ):
            raise InvalidExecutionReportError(
                "successful ExecutionReport requires a staged output digest"
            )
    elif (
        outputs["temporary_ref"] is not None
        or outputs["records_sha256"] is not None
        or counts["records"] != 0
        or counts["output_bytes"] != 0
    ):
        raise InvalidExecutionReportError(
            "non-success ExecutionReport cannot expose partial output"
        )
    return report


def request_identities(binding: ExecutionBinding) -> dict[str, str]:
    return {
        "source_id": binding.source_id,
        "source_revision": binding.source_revision,
        "bot_id": binding.bot_id,
        "build_id": binding.build_id,
        "bot_build_manifest_digest": binding.bot_build_manifest_digest,
        "assembly_id": binding.assembly_id,
        "catalog_digest": binding.catalog_digest,
        "oci_digest": binding.oci_digest,
        "local_image_id": binding.local_image_id,
        "fixture_bundle_id": binding.fixture_bundle_id,
        "fixture_bundle_digest": binding.fixture_bundle_digest,
    }
