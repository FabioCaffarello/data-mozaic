"""Protected Linux E2E: BC04 Run -> Docker adapter -> Scrapy Collector -> report."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from execution_coordination.adapters.scrapy_container import (
    ExecutorConfig,
    SandboxLimits,
    ScrapyContainerExecutor,
)
from execution_coordination.application import ExecutionCoordinator
from execution_coordination.domain import CoordinationError, ExecutionBinding, State
from execution_coordination.repository import InMemoryRunRepository
from mozaic_bot_build import canonical_json
from run_scrapy_collector_multibot import ROOT, _check_records, _prepare_catalog
from scrapy_collector.catalog.model import load_catalog


def _run(argv: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, check=True, capture_output=True, text=True, timeout=timeout
    )


def _build_image(assembled: Path, temp: Path) -> tuple[str, str, str]:
    assembly = json.loads((assembled / "assembly.json").read_text())
    tag = f"data-mozaic/execution-coordination-{uuid.uuid4().hex[:12]}:test"
    metadata_path = temp / "build-metadata.json"
    oci_archive = temp / "scrapy-collector.oci.tar"
    local_image_id: str | None = None
    try:
        _run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64",
                "--output",
                f"type=oci,dest={oci_archive}",
                "--metadata-file",
                str(metadata_path),
                "--build-arg",
                f"CATALOG_ASSEMBLY_ID={assembly['assembly_id']}",
                "--build-context",
                f"botcatalog={assembled}",
                "--file",
                str(ROOT / "apps/workers/scrapy-collector/Dockerfile"),
                str(ROOT),
            ],
            timeout=600,
        )
        metadata = json.loads(metadata_path.read_text())
        with tarfile.open(oci_archive) as archive:
            index = json.load(archive.extractfile("index.json"))
        manifests = [
            item
            for item in index.get("manifests", [])
            if item.get("platform") == {"architecture": "amd64", "os": "linux"}
        ]
        if len(manifests) != 1:
            raise RuntimeError("OCI archive must contain exactly linux/amd64")
        oci_digest = manifests[0].get("digest")
        if (
            not isinstance(oci_digest, str)
            or not oci_digest.startswith("sha256:")
            or metadata.get("containerimage.digest") != oci_digest
        ):
            raise RuntimeError("BuildKit metadata differs from OCI manifest digest")
        loaded = _run(["docker", "load", "--input", str(oci_archive)]).stdout
        local_image_id = next(
            (
                line.split(": ", 1)[1]
                for line in loaded.splitlines()
                if line.startswith("Loaded image ID: ")
            ),
            None,
        )
        if not isinstance(local_image_id, str) or not local_image_id.startswith(
            "sha256:"
        ):
            raise RuntimeError("Docker did not load an OCI image ID")
        _run(["docker", "image", "tag", local_image_id, tag])
        info = json.loads(_run(["docker", "image", "inspect", tag]).stdout)[0]
        if info.get("Id") != local_image_id:
            raise RuntimeError(
                "Docker image tag resolved to a different local image ID"
            )
    except Exception:
        subprocess.run(
            ["docker", "image", "rm", "--force", tag],
            check=False,
            capture_output=True,
            text=True,
        )
        if local_image_id:
            subprocess.run(
                ["docker", "image", "rm", "--force", local_image_id],
                check=False,
                capture_output=True,
                text=True,
            )
        raise
    repository = tag.rsplit(":", 1)[0]
    image_ref = f"{repository}@{oci_digest}"
    return tag, image_ref, local_image_id


def _binding(
    catalog_root: Path, image_ref: str, image_id: str, entry: dict, duration: int
) -> ExecutionBinding:
    catalog = load_catalog(catalog_root)
    selected = catalog.resolve(entry["bot_id"], entry["build_id"])
    manifest_bytes = (catalog.build_root(selected) / "manifest.json").read_bytes()
    fixture_digest = hashlib.sha256(
        canonical_json(list(selected.input_fixtures))
    ).hexdigest()
    return ExecutionBinding(
        source_id=selected.source["source_id"],
        source_revision=selected.source["source_revision"],
        bot_id=selected.bot_id,
        build_id=selected.build_id,
        bot_build_manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
        assembly_id=catalog.assembly_id,
        catalog_digest=hashlib.sha256(
            (catalog.root / "catalog.json").read_bytes()
        ).hexdigest(),
        oci_image=image_ref,
        oci_digest=image_ref.rsplit("@", 1)[1],
        local_image_id=image_id,
        fixture_bundle_id=selected.build_id,
        fixture_bundle_digest=fixture_digest,
        max_duration_seconds=duration,
    )


def _entry_for(
    assembled: Path, entries: list[dict], revision: str | None = None
) -> dict:
    catalog = load_catalog(assembled)
    for entry in entries:
        selected = catalog.resolve(entry["bot_id"], entry["build_id"])
        manifest = json.loads(
            (catalog.build_root(selected) / "manifest.json").read_text()
        )
        if revision is not None and manifest["source"]["source_revision"] == revision:
            return entry
        if (
            revision is None
            and manifest["playbook_id"] == "html-simple-catalog"
            and manifest["source"]["source_revision"] == "fixture-1"
        ):
            return entry
    raise RuntimeError(f"required catalog entry not found: {revision}")


def _wait_for_handle(
    coordinator: ExecutionCoordinator, run_id: str, timeout: float = 10
):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        handle = coordinator._handles.get(run_id)
        if handle is not None:
            return handle
        time.sleep(0.02)
    raise RuntimeError("container did not reach supervised running state")


def _start(
    coordinator: ExecutionCoordinator,
    binding: ExecutionBinding,
    key: str,
    deadline_seconds: int = 30,
):
    deadline = datetime.now(UTC) + timedelta(seconds=deadline_seconds)
    return coordinator.submit(
        idempotency_scope="fixture-e2e:tenant-a",
        idempotency_key=key,
        request_id=f"request-{key}",
        binding=binding,
        deadline=deadline,
    )


def _assert_failed_without_output(run, output_root: Path, expected: State) -> None:
    if run.state != expected:
        raise RuntimeError(f"expected {expected}, got {run.state}: {run.attempt}")
    if run.attempt and run.attempt.report and run.attempt.report.output_ref:
        raise RuntimeError("failed Attempt returned an output reference")
    if (output_root / run.run_id / run.attempt.attempt_id).exists():
        raise RuntimeError("failed Attempt published output")


def _remove_task_temp_tree(root: Path) -> None:
    """Remove only this harness-created temporary root, including read-only builds."""
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("E2E temporary root changed identity before cleanup")
    for directory, child_dirs, files in os.walk(root, topdown=False, followlinks=False):
        current = Path(directory)
        for name in files:
            path = current / name
            if path.is_symlink():
                path.unlink()
            else:
                path.chmod(0o600)
        for name in child_dirs:
            path = current / name
            if path.is_symlink():
                path.unlink()
            else:
                path.chmod(0o700)
        current.chmod(0o700)
    shutil.rmtree(root)


def main() -> int:
    if os.name != "posix":
        raise RuntimeError(
            "protected E2E requires a POSIX host and Linux container runtime"
        )
    temp = Path(tempfile.mkdtemp(prefix=".execution-coordination-e2e-", dir=ROOT))
    tag: str | None = None
    local_image_id: str | None = None
    coordinator: ExecutionCoordinator | None = None
    active_runs: list[str] = []
    try:
        assembled, entries, _ = _prepare_catalog(
            temp / "catalog", "linux/amd64", include_lifecycle_tests=True
        )
        tag, image_ref, local_image_id = _build_image(assembled, temp)
        catalog = load_catalog(assembled)

        def entry_for(playbook_id: str, revision: str) -> dict:
            for candidate in entries:
                selected = catalog.resolve(candidate["bot_id"], candidate["build_id"])
                manifest = json.loads(
                    (catalog.build_root(selected) / "manifest.json").read_text()
                )
                if (
                    manifest["playbook_id"] == playbook_id
                    and manifest["source"]["source_revision"] == revision
                ):
                    return candidate
            raise RuntimeError(f"missing {playbook_id}/{revision}")

        html = entry_for("html-simple-catalog", "fixture-1")
        html_v2 = entry_for("html-simple-catalog", "fixture-2")
        callback = entry_for("html-simple-catalog", "callback-failure-test")
        slow = entry_for("html-simple-catalog", "slow-callback-test")
        timeout_entry = entry_for("html-simple-catalog", "timeout-callback-test")
        multipage_entry = next(
            e
            for e in entries
            if catalog.resolve(e["bot_id"], e["build_id"]).playbook_id
            == "bounded-multipage-catalog"
        )
        json_entry = next(
            e
            for e in entries
            if catalog.resolve(e["bot_id"], e["build_id"]).playbook_id
            == "synthetic-json-api"
        )
        if (
            html["bot_id"] != html_v2["bot_id"]
            or html["build_id"] == html_v2["build_id"]
        ):
            raise RuntimeError(
                "same logical bot versions did not coexist by exact build identity"
            )
        output_root = temp / "private-results"
        trusted = {}
        for entry, cap in (
            (html, 30),
            (html_v2, 30),
            (multipage_entry, 30),
            (json_entry, 30),
            (callback, 30),
            (slow, 30),
            (timeout_entry, 2),
        ):
            trusted[(entry["bot_id"], entry["build_id"])] = _binding(
                assembled, image_ref, local_image_id, entry, cap
            )
        timeout_binding = trusted[(timeout_entry["bot_id"], timeout_entry["build_id"])]
        executor = ScrapyContainerExecutor(
            ExecutorConfig(
                trusted_bindings=trusted,
                output_root=output_root,
                limits=SandboxLimits(cpus=1.0, memory_bytes=536_870_912, pids=64),
            )
        )
        coordinator = ExecutionCoordinator(
            InMemoryRunRepository(), executor, max_active=2
        )

        html_run = _start(
            coordinator, trusted[(html["bot_id"], html["build_id"])], "html"
        )
        duplicate = coordinator.submit(
            idempotency_scope="fixture-e2e:tenant-a",
            idempotency_key="html",
            request_id="request-html",
            binding=trusted[(html["bot_id"], html["build_id"])],
            deadline=html_run.deadline,
        )
        if duplicate.run_id != html_run.run_id:
            raise RuntimeError("duplicate idempotent submit created a second Run")
        active_runs.append(html_run.run_id)
        html_result = coordinator.wait(html_run.run_id, timeout=90)
        if (
            html_result.state != State.SUCCEEDED
            or not html_result.terminal_report
            or not html_result.terminal_report.output_ref
        ):
            raise RuntimeError(f"HTML Run did not succeed: {html_result}")
        _check_records(
            Path(html_result.terminal_report.output_ref),
            ROOT / "examples/playbooks/html-simple/expected-records.jsonl",
            source_revision="fixture-1",
        )

        html_v2_run = _start(
            coordinator,
            trusted[(html_v2["bot_id"], html_v2["build_id"])],
            "html-version-2",
        )
        active_runs.append(html_v2_run.run_id)
        html_v2_result = coordinator.wait(html_v2_run.run_id, timeout=90)
        if (
            html_v2_result.state != State.SUCCEEDED
            or not html_v2_result.terminal_report
        ):
            raise RuntimeError(
                f"second exact HTML build did not succeed: {html_v2_result}"
            )
        _check_records(
            Path(html_v2_result.terminal_report.output_ref),
            ROOT / "examples/playbooks/html-simple/expected-records.jsonl",
            source_revision="fixture-2",
        )

        multipage_run = _start(
            coordinator,
            trusted[(multipage_entry["bot_id"], multipage_entry["build_id"])],
            "multipage",
        )
        active_runs.append(multipage_run.run_id)
        multipage_result = coordinator.wait(multipage_run.run_id, timeout=90)
        if (
            multipage_result.state != State.SUCCEEDED
            or not multipage_result.terminal_report
        ):
            raise RuntimeError(f"multipage Run did not succeed: {multipage_result}")
        _check_records(
            Path(multipage_result.terminal_report.output_ref),
            ROOT / "examples/playbooks/multipage-catalog/expected-records.jsonl",
        )

        json_run = _start(
            coordinator, trusted[(json_entry["bot_id"], json_entry["build_id"])], "json"
        )
        active_runs.append(json_run.run_id)
        json_result = coordinator.wait(json_run.run_id, timeout=90)
        if json_result.state != State.SUCCEEDED or not json_result.terminal_report:
            raise RuntimeError(f"JSON Run did not succeed: {json_result}")
        _check_records(
            Path(json_result.terminal_report.output_ref),
            ROOT / "examples/playbooks/json-api/expected-records.jsonl",
        )

        callback_run = _start(
            coordinator,
            trusted[(callback["bot_id"], callback["build_id"])],
            "callback-failure",
        )
        active_runs.append(callback_run.run_id)
        callback_result = coordinator.wait(callback_run.run_id, timeout=90)
        _assert_failed_without_output(callback_result, output_root, State.FAILED)
        if not callback_result.terminal_report or "TEST_CALLBACK_EXCEPTION" not in str(
            callback_result.terminal_report.diagnostics
        ):
            raise RuntimeError(
                "real Scrapy callback failure was not reflected in diagnostics"
            )

        timeout_run = _start(
            coordinator, timeout_binding, "timeout", deadline_seconds=30
        )
        active_runs.append(timeout_run.run_id)
        timeout_result = coordinator.wait(timeout_run.run_id, timeout=90)
        _assert_failed_without_output(timeout_result, output_root, State.TIMED_OUT)

        cancel_run = _start(
            coordinator, trusted[(slow["bot_id"], slow["build_id"])], "cancel"
        )
        active_runs.append(cancel_run.run_id)
        cancel_handle = _wait_for_handle(coordinator, cancel_run.run_id)
        # The live container proves effective network isolation and compiler absence.
        probe = _run(
            [
                "docker",
                "exec",
                "--user",
                "65532:65532",
                cancel_handle.container_id,
                "python",
                "-I",
                "-c",
                "import importlib.util,socket; assert importlib.util.find_spec('collector_engineering') is None;\ntry: socket.create_connection(('1.1.1.1',443),timeout=1)\nexcept OSError: pass\nelse: raise AssertionError('network egress available')",
            ],
            timeout=10,
        )
        if probe.returncode:
            raise RuntimeError("network denial or compiler absence probe failed")
        coordinator.cancel(cancel_run.run_id)
        cancel_result = coordinator.wait(cancel_run.run_id, timeout=30)
        _assert_failed_without_output(cancel_result, output_root, State.CANCELLED)

        crash_run = _start(
            coordinator, trusted[(slow["bot_id"], slow["build_id"])], "crash"
        )
        active_runs.append(crash_run.run_id)
        crash_handle = _wait_for_handle(coordinator, crash_run.run_id)
        _run(
            ["docker", "kill", "--signal", "KILL", crash_handle.container_id],
            timeout=10,
        )
        crash_result = coordinator.wait(crash_run.run_id, timeout=30)
        _assert_failed_without_output(crash_result, output_root, State.CRASHED)

        followup = _start(
            coordinator,
            trusted[(html["bot_id"], html["build_id"])],
            "healthy-after-crash",
        )
        active_runs.append(followup.run_id)
        healthy = coordinator.wait(followup.run_id, timeout=90)
        if healthy.state != State.SUCCEEDED:
            raise RuntimeError(
                f"healthy execution after failures did not succeed: {healthy.state}"
            )
        expected_outputs = {
            (html_result.run_id, html_result.attempt.attempt_id),
            (html_v2_result.run_id, html_v2_result.attempt.attempt_id),
            (multipage_result.run_id, multipage_result.attempt.attempt_id),
            (json_result.run_id, json_result.attempt.attempt_id),
            (healthy.run_id, healthy.attempt.attempt_id),
        }
        actual_outputs = {
            (run_dir.name, attempt_dir.name)
            for run_dir in output_root.iterdir()
            if run_dir.is_dir()
            for attempt_dir in run_dir.iterdir()
            if attempt_dir.is_dir()
        }
        if actual_outputs != expected_outputs:
            raise RuntimeError(f"successful output set differs: {actual_outputs}")
        print(
            json.dumps(
                {
                    "state": "passed",
                    "runs": {
                        "html": str(html_result.state),
                        "html_version_2": str(html_v2_result.state),
                        "multipage": str(multipage_result.state),
                        "json": str(json_result.state),
                        "callback": str(callback_result.state),
                        "timeout": str(timeout_result.state),
                        "cancel": str(cancel_result.state),
                        "crash": str(crash_result.state),
                        "healthy_after_crash": str(healthy.state),
                    },
                    "assembly_id": catalog.assembly_id,
                    "local_image_id": local_image_id,
                    "oci_digest": image_ref.rsplit("@", 1)[1],
                    "outputs": len(actual_outputs),
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if coordinator is not None:
            for run_id in active_runs:
                try:
                    current = coordinator.get(run_id)
                    if current.state not in {
                        State.SUCCEEDED,
                        State.FAILED,
                        State.TIMED_OUT,
                        State.CANCELLED,
                        State.CRASHED,
                    }:
                        coordinator.cancel(run_id)
                        coordinator.wait(run_id, timeout=20)
                except (CoordinationError, TimeoutError, RuntimeError) as error:
                    print(
                        f"scoped cleanup did not complete for {run_id}: {type(error).__name__}"
                    )
            coordinator.shutdown(wait=True)
        if tag:
            subprocess.run(
                ["docker", "image", "rm", "--force", tag],
                check=False,
                capture_output=True,
                text=True,
            )
        if local_image_id:
            subprocess.run(
                ["docker", "image", "rm", "--force", local_image_id],
                check=False,
                capture_output=True,
                text=True,
            )
        _remove_task_temp_tree(temp)


if __name__ == "__main__":
    raise SystemExit(main())
