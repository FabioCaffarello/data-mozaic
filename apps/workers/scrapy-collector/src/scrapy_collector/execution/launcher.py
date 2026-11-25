"""Exact BotBuild resolution and one-fresh-process-per-crawl lifecycle."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from mozaic_bot_build import BotBuildVerificationError, verify_bot_build

from scrapy_collector.catalog.model import BotCatalog, CatalogEntry, runtime_platform_id
from scrapy_collector.execution.context import ExecutionContext

_MAX_DIAGNOSTIC_BYTES = 1_000_000
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_MAX_RESULT_RECORDS = 100_000


class ExecutionError(RuntimeError):
    """A selected crawl failed before a complete output was published."""


def _secure_directory(path: Path, *, create: bool) -> Path:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ExecutionError("[OUTPUT_PATH_UNSAFE] output path contains a symlink")
    if create:
        absolute.mkdir(parents=True, exist_ok=True)
    if not absolute.is_dir():
        raise ExecutionError("[OUTPUT_PATH_UNSAFE] output root is not a directory")
    return absolute


def _verify_selected(catalog: BotCatalog, entry: CatalogEntry) -> Path:
    build_root = catalog.build_root(entry)
    try:
        verified = verify_bot_build(
            build_root,
            entry.build_manifest_digest,
            supported_versions=("bot-build-manifest/v2",),
        )
    except BotBuildVerificationError as error:
        raise ExecutionError(str(error)) from error
    manifest = verified.manifest
    compatibility = manifest.get("compatibility", {})
    current_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    if (
        manifest.get("build_id") != entry.build_id
        or manifest.get("bot_id") != entry.bot_id
        or manifest.get("source") != entry.source
        or manifest.get("playbook_id") != entry.playbook_id
        or manifest.get("ir_sha256") != entry.ir_sha256
        or manifest.get("backend_id") != "scrapy-fixture"
        or manifest.get("backend_version") != entry.backend_version
        or manifest.get("capability_profile_id") != "scrapy-fixture-v1"
        or manifest.get("runtime_identity") != entry.runtime_identity
        or manifest.get("compatibility") != entry.compatibility
        or manifest.get("input_fixtures") != list(entry.input_fixtures)
        or compatibility.get("worker_contract") != "scrapy-collector/v1"
        or compatibility.get("execution_mode") != "fixture-only"
        or compatibility.get("target_platform") != catalog.target_platform
        or catalog.target_platform != runtime_platform_id()
        or compatibility.get("python_minor") != current_minor
        or compatibility.get("scrapy") != importlib.metadata.version("scrapy")
        or compatibility.get("scrapy_runtime")
        not in {"scrapy-fixture-runtime/1.0.0", "scrapy-fixture-runtime/1.1.0"}
        or manifest.get("dependency_identity", {}).get("lockfile_sha256")
        != catalog.dependency_lock_sha256
    ):
        raise ExecutionError(
            "[BUILD_COMPATIBILITY_UNSUPPORTED] BotBuild is incompatible with this worker"
        )
    profile_path = build_root / "capability-profile.json"
    try:
        profile = json.loads(
            profile_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_pairs,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ExecutionError(
            "[BUILD_PROFILE_INVALID] profile file is invalid"
        ) from error
    from mozaic_bot_build.verification import canonical_json

    if hashlib.sha256(canonical_json(profile)).hexdigest() != manifest.get(
        "capability_profile_sha256"
    ):
        raise ExecutionError(
            "[BUILD_PROFILE_DIGEST_MISMATCH] capability profile differs"
        )
    if tuple(profile.get("capabilities", ())) != entry.capabilities:
        raise ExecutionError(
            "[BUILD_PROFILE_MISMATCH] catalog capabilities differ from profile"
        )
    return build_root


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_result(
    result_dir: Path, entry: CatalogEntry
) -> tuple[dict[str, Any], bytes]:
    if result_dir.is_symlink() or not result_dir.is_dir():
        raise ExecutionError("[EXECUTION_OUTPUT_INVALID] result directory is unsafe")
    allowed = {"records.jsonl", "report.json", "raw-references.json"}
    files = list(result_dir.iterdir())
    actual = {path.name for path in files}
    if (
        not actual.issubset(allowed)
        or not {"records.jsonl", "report.json"}.issubset(actual)
        or any(path.is_symlink() or not path.is_file() for path in files)
    ):
        raise ExecutionError("[EXECUTION_OUTPUT_INVALID] result file set is invalid")
    records_path = result_dir / "records.jsonl"
    report_path = result_dir / "report.json"
    if (
        records_path.stat().st_size > _MAX_RESULT_BYTES
        or report_path.stat().st_size > 1_000_000
        or (result_dir / "raw-references.json").exists()
        and (result_dir / "raw-references.json").stat().st_size > 1_000_000
    ):
        raise ExecutionError(
            "[EXECUTION_OUTPUT_LIMIT_EXCEEDED] result exceeds worker limits"
        )
    records_bytes = records_path.read_bytes()
    try:
        report = json.loads(
            report_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_json_pairs,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ExecutionError(
            "[EXECUTION_REPORT_INVALID] report is not valid JSON"
        ) from error
    if (
        not isinstance(report, dict)
        or report.get("state") != "passed"
        or report.get("records_file") != "records.jsonl"
        or report.get("records_sha256") != hashlib.sha256(records_bytes).hexdigest()
    ):
        raise ExecutionError(
            "[EXECUTION_REPORT_INVALID] report does not verify records"
        )
    fixtures = {
        (item.get("url"), item.get("evidence_ref")): item
        for item in entry.input_fixtures
    }
    observed_pages: set[tuple[str, str, str]] = set()
    lines = records_bytes.splitlines(keepends=True)
    if len(lines) > _MAX_RESULT_RECORDS or report.get("records") != len(lines):
        raise ExecutionError(
            "[EXECUTION_OUTPUT_LIMIT_EXCEEDED] record count is invalid"
        )
    for line in lines:
        if not line.endswith(b"\n") or len(line) > 1_000_000:
            raise ExecutionError(
                "[EXECUTION_RECORD_INVALID] record line is malformed or too large"
            )
        try:
            record = json.loads(line, object_pairs_hook=_unique_json_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ExecutionError(
                "[EXECUTION_RECORD_INVALID] record is not valid JSON"
            ) from error
        if not isinstance(record, dict) or not isinstance(
            record.get("_provenance"), dict
        ):
            raise ExecutionError(
                "[EXECUTION_PROVENANCE_INVALID] record lacks provenance"
            )
        provenance = record["_provenance"]
        url = provenance.get("source_url")
        evidence_ref = provenance.get("evidence_ref")
        fixture = fixtures.get((url, evidence_ref))
        page_index = provenance.get("page_index")
        record_index = provenance.get("record_index")
        if (
            fixture is None
            or provenance.get("source_id") != entry.source["source_id"]
            or provenance.get("source_revision") != entry.source["source_revision"]
            or provenance.get("requested_url") != url
            or provenance.get("effective_url") != url
            or provenance.get("raw_sha256") != fixture.get("sha256")
            or not isinstance(provenance.get("operation_id"), str)
            or not isinstance(page_index, int)
            or isinstance(page_index, bool)
            or page_index < 0
            or not isinstance(record_index, int)
            or isinstance(record_index, bool)
            or record_index < 0
        ):
            raise ExecutionError(
                "[EXECUTION_PROVENANCE_INVALID] provenance differs from BotBuild fixtures"
            )
        observed_pages.add((url, evidence_ref, fixture["sha256"]))
    raw_path = result_dir / "raw-references.json"
    if raw_path.exists():
        try:
            raw_refs = json.loads(
                raw_path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_pairs,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ExecutionError(
                "[EXECUTION_RAW_REFERENCES_INVALID] raw references are invalid"
            ) from error
        if not isinstance(raw_refs, list) or any(
            not isinstance(item, dict) for item in raw_refs
        ):
            raise ExecutionError(
                "[EXECUTION_RAW_REFERENCES_INVALID] raw references are invalid"
            )
        actual_refs = {
            (item.get("url"), item.get("evidence_ref"), item.get("sha256"))
            for item in raw_refs
        }
        known_refs = {
            (item.get("url"), item.get("evidence_ref"), item.get("sha256"))
            for item in entry.input_fixtures
        }
        if not observed_pages.issubset(actual_refs) or not actual_refs.issubset(
            known_refs
        ):
            raise ExecutionError(
                "[EXECUTION_RAW_REFERENCES_INVALID] raw references differ from provenance"
            )
    return report, records_bytes


def run(
    catalog: BotCatalog,
    bot_id: str,
    build_id: str,
    context: ExecutionContext,
) -> dict[str, Any]:
    """Run one verified build in a clean process and atomically publish success."""
    entry = catalog.resolve(bot_id, build_id)
    _verify_selected(catalog, entry)
    fixtures = catalog.fixture_root(entry)
    if fixtures.is_symlink() or not fixtures.is_dir():
        raise ExecutionError(
            "[FIXTURE_ROOT_INVALID] assembled fixture directory is unavailable"
        )
    output_root = _secure_directory(context.output_root, create=True)
    execution_id = str(uuid.uuid4())
    final_output = output_root / execution_id
    work_root = output_root / f".{execution_id}.work"
    if final_output.exists() or final_output.is_symlink() or work_root.exists():
        raise ExecutionError(
            "[EXECUTION_PATH_COLLISION] execution output already exists"
        )
    work_root.mkdir(mode=0o700)
    temp_root = work_root / "tmp"
    temp_root.mkdir(mode=0o700)
    stdout_path = temp_root / "stdout.log"
    stderr_path = temp_root / "stderr.log"
    command = [
        sys.executable,
        "-I",
        "-B",
        "-m",
        "scrapy_collector.execution.child",
        "--catalog-root",
        str(catalog.root),
        "--bot-id",
        entry.bot_id,
        "--build-id",
        entry.build_id,
        "--stage-root",
        str(work_root),
    ]
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "SCRAPY_SETTINGS_MODULE": "scrapy_collector.settings",
        "SCRAPY_COLLECTOR_ASSEMBLY_ID": catalog.assembly_id,
        "TMPDIR": str(temp_root),
    }
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    state = "failed"
    diagnostic: str | None = None
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            process = subprocess.Popen(
                command,
                cwd=work_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            previous_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGTERM, _interrupt_wait)
            signal.signal(signal.SIGINT, _interrupt_wait)
            try:
                exit_code = process.wait(timeout=context.timeout_seconds)
            except subprocess.TimeoutExpired:
                state = "timed_out"
                diagnostic = "[EXECUTION_TIMEOUT] deadline exceeded"
                _terminate(process, context.termination_grace_seconds)
                exit_code = process.returncode
            except KeyboardInterrupt:
                state = "cancelled"
                diagnostic = "[EXECUTION_CANCELLED] launcher interrupted"
                _terminate(process, context.termination_grace_seconds)
                exit_code = process.returncode
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)
                signal.signal(signal.SIGINT, previous_sigint)
        duration = time.monotonic() - started
        if state == "failed":
            output = _read_bounded(stdout_path)
            errors = _read_bounded(stderr_path)
            result_dir = work_root / "result"
            report_path = result_dir / "report.json"
            records_path = result_dir / "records.jsonl"
            if exit_code != 0:
                diagnostic = _diagnostic(output, errors, exit_code)
            elif not report_path.is_file() or not records_path.is_file():
                diagnostic = "[EXECUTION_OUTPUT_INCOMPLETE] successful process did not produce report and records"
            else:
                try:
                    report, _records = _validate_result(result_dir, entry)
                except (ExecutionError, OSError, ValueError) as error:
                    diagnostic = str(error)
                else:
                    os.replace(result_dir, final_output)
                    state = "succeeded"
                    return {
                        "state": state,
                        "execution_id": execution_id,
                        "bot_id": entry.bot_id,
                        "build_id": entry.build_id,
                        "output_dir": str(final_output),
                        "duration_seconds": round(duration, 6),
                        "report": report,
                    }
        return {
            "state": state,
            "execution_id": execution_id,
            "bot_id": entry.bot_id,
            "build_id": entry.build_id,
            "duration_seconds": round(time.monotonic() - started, 6),
            "diagnostic": diagnostic,
        }
    finally:
        if process is not None and process.poll() is None:
            _terminate(process, context.termination_grace_seconds)
        shutil.rmtree(work_root, ignore_errors=True)


def _terminate(process: subprocess.Popen[bytes], grace_seconds: float) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _interrupt_wait(signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def _read_bounded(path: Path) -> str:
    try:
        data = path.read_bytes()[-_MAX_DIAGNOSTIC_BYTES:]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _diagnostic(stdout: str, stderr: str, exit_code: int) -> str:
    combined = "\n".join(line for line in (stderr.strip(), stdout.strip()) if line)
    if not combined:
        return f"[EXECUTION_CHILD_FAILED] child exited with status {exit_code}"
    return combined[-_MAX_DIAGNOSTIC_BYTES:]
