"""Run bounded end-to-end raw artifact workloads and sample component memory."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SIZES = (65_536, 1_000_000, 2_000_000)
MEMORY = re.compile(r"^([0-9.]+)(B|kB|KB|KiB|MB|MiB|GB|GiB)$")
UNITS = {
    "B": 1,
    "kB": 1_000,
    "KB": 1_000,
    "KiB": 1 << 10,
    "MB": 1_000_000,
    "MiB": 1 << 20,
    "GB": 1_000_000_000,
    "GiB": 1 << 30,
}


def parse_memory(value: str) -> int | None:
    match = MEMORY.fullmatch(value.strip())
    if match is None:
        return None
    return int(float(match.group(1)) * UNITS[match.group(2)])


def sample_containers(project_prefix: str) -> dict[str, int]:
    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    observations: dict[str, int] = {}
    if result.returncode:
        return observations
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(row.get("Name", ""))
        if not (
            name.startswith(project_prefix)
            or "artifact-custody" in name
            or "rustfs" in name
        ):
            continue
        usage = str(row.get("MemUsage", "")).split(" /", 1)[0]
        measured = parse_memory(usage)
        if measured is not None:
            observations[name] = max(observations.get(name, 0), measured)
    return observations


def sample_host_rss(pid: int) -> int | None:
    result = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip()) * 1024 if result.returncode == 0 else None
    except ValueError:
        return None


def run_one(
    size: int,
    repetition: int,
    interval: float,
    isolation_id: str | None = None,
) -> dict[str, object]:
    project = "data-mozaic-authorized-live-raw-pdf"
    command = [
        sys.executable,
        str(ROOT / "tooling/run_authorized_live_json.py"),
        "--case",
        "raw-pdf",
        "--artifact-size",
        str(size),
    ]
    if isolation_id is not None:
        command.extend(["--isolation-id", isolation_id])
    temporary_root = tempfile.TemporaryDirectory(prefix="dm-resource-run-")
    child_env = os.environ.copy()
    child_env["TMPDIR"] = temporary_root.name
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    started = time.monotonic()
    peaks: dict[str, int] = {}
    host_peak = 0
    samples = 0
    lines: list[str] = []
    while process.poll() is None:
        observations = sample_containers(project)
        for name, value in observations.items():
            peaks[name] = max(peaks.get(name, 0), value)
        host_rss = sample_host_rss(process.pid)
        if host_rss is not None:
            host_peak = max(host_peak, host_rss)
        samples += 1
        time.sleep(interval)
    output, _ = process.communicate()
    finished = time.monotonic()
    residual_temporary_bytes = sum(
        path.stat().st_size
        for path in Path(temporary_root.name).rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    temporary_root.cleanup()
    if output:
        lines = output.splitlines()
    elapsed = time.monotonic() - started
    if process.returncode:
        raise RuntimeError(
            f"controlled acquisition failed for size={size}, repetition={repetition}, "
            f"exit={process.returncode}\n" + "\n".join(lines[-100:])
        )
    success = next(
        (
            line
            for line in lines
            if line.startswith("AUTHORIZED_CONTROLLED_LIVE_RAW-PDF_ACCEPTED ")
        ),
        None,
    )
    if success is None:
        raise RuntimeError(
            "acquisition command succeeded without its raw acceptance record"
        )
    result = json.loads(success.split(" ", 1)[1])
    if result.get("state") != "succeeded" or len(result.get("artifact_ids", [])) != 1:
        raise RuntimeError("raw acquisition did not publish exactly one artifact")
    return {
        "artifact_size_bytes": size,
        "repetition": repetition,
        "isolation_id": isolation_id,
        "started_monotonic_seconds": started,
        "finished_monotonic_seconds": finished,
        "elapsed_seconds": elapsed,
        "sample_interval_seconds": interval,
        "sample_count": samples,
        "host_acceptance_process_sampled_rss_peak_bytes": host_peak,
        "container_sampled_memory_peak_bytes": peaks,
        "build_id": result.get("build_id"),
        "runtime_image_digest": result.get("runtime_image_digest"),
        "release_id": result.get("release_id"),
        "run_id": result.get("run_id"),
        "attempt_id": result.get("attempt_id"),
        "fetch_ids": result.get("fetch_ids"),
        "artifact_ids": result.get("artifact_ids"),
        "artifact_digests": result.get("artifact_digests"),
        "output_set_id": result.get("output_set_id"),
        "manifest_digest": result.get("manifest_digest"),
        "publication_receipt": result.get("publication_receipt"),
        "bytes_transferred": size,
        "gateway_spool_start": result.get("gateway_resource_start_snapshot"),
        "gateway_spool_end": result.get("gateway_resource_snapshot"),
        "gateway_spool_bytes_added": (
            result["gateway_resource_snapshot"]["spool_bytes_used"]
            - result["gateway_resource_start_snapshot"]["spool_bytes_used"]
            if isinstance(result.get("gateway_resource_snapshot"), dict)
            and isinstance(result.get("gateway_resource_start_snapshot"), dict)
            else None
        ),
        "acceptance_temporary_residual_bytes_before_cleanup": residual_temporary_bytes,
        "measurement": "sampled Docker stats memory usage (cgroup/container view) and host process RSS via ps; not an exact peak",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=DEFAULT_SIZES)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--concurrent-count",
        type=int,
        default=2,
        help="also run this many isolated full-path acquisitions concurrently",
    )
    parser.add_argument(
        "--concurrent-size",
        type=int,
        default=65_536,
        help="artifact size for each concurrent acquisition (1..2,000,000 bytes)",
    )
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        any(size < 1 or size > 2_000_000 for size in args.sizes)
        or not 1 <= args.concurrent_size <= 2_000_000
        or not 1 <= args.repetitions <= 5
        or not 2 <= args.concurrent_count <= 4
        or not 0.1 <= args.sample_interval <= 10
    ):
        parser.error("sizes, repetitions or sampling interval exceed benchmark bounds")
    docker = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    output_path = args.output or (
        ROOT
        / ".local"
        / (
            "acquisition-resource-benchmark-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + ".json"
        )
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "schema": "data-mozaic/acquisition-resource-benchmark/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "environment": {
            "platform": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "docker_server": docker,
            "configuration": {
                "profile_limit_bytes": 2_000_000,
                "per_response_bytes": "exact workload size, <= historical profile limit",
                "aggregate_run_bytes": "max(200000, workload size)",
                "gateway_memory_limit": "256 MiB (Compose cgroup limit)",
                "worker_memory_limit": "64 MiB (Compose cgroup limit)",
                "gateway_spool_limit": "64 MiB (Compose configuration)",
                "BC06_max_upload_bytes": "existing configured provider bound; see artifact-custody configuration",
                "concurrent_artifact_size_bytes": args.concurrent_size,
                "concurrent_count": args.concurrent_count,
                "repetitions_per_sequential_size": args.repetitions,
            },
        },
        "workloads": [],
        "concurrent_workloads": [],
        "sampling_limits": [
            "Docker stats reports sampled container/cgroup usage and may include cache; it is not process RSS or an exact peak.",
            "Host RSS sample covers the acquisition harness process that performs importer work.",
            "Sampling can miss short-lived peaks and command polling perturbs execution slightly.",
            "Gateway spool usage is an authenticated component snapshot before and after the operation; the maximum intermediate usage is bounded by reserved-byte accounting, not sampled independently.",
        ],
    }
    workloads = report["workloads"]
    assert isinstance(workloads, list)
    for size in args.sizes:
        for repetition in range(1, args.repetitions + 1):
            workloads.append(run_one(size, repetition, args.sample_interval))
            output_path.write_text(json.dumps(report, indent=2) + "\n")
    concurrent_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.concurrent_count) as executor:
        concurrent = list(
            executor.map(
                lambda index: run_one(
                    args.concurrent_size,
                    index + 1,
                    args.sample_interval,
                    format(index + 1, "x"),
                ),
                range(args.concurrent_count),
            )
        )
    concurrent_finished = time.monotonic()
    concurrent_workloads = report["concurrent_workloads"]
    assert isinstance(concurrent_workloads, list)
    interval_overlap = max(
        float(item["started_monotonic_seconds"]) for item in concurrent
    ) < min(float(item["finished_monotonic_seconds"]) for item in concurrent)
    concurrent_workloads.append(
        {
            "concurrency": args.concurrent_count,
            "artifact_size_bytes_each": args.concurrent_size,
            "group_elapsed_seconds": concurrent_finished - concurrent_started,
            "acquisition_process_interval_overlap_confirmed": interval_overlap,
            "acquisitions": concurrent,
            "limitation": "process and gateway request intervals are concurrent; per-fetch overlap is also exercised independently by gateway resource tests; this controlled workload is not a production throughput certification",
        }
    )
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    report["finished_at"] = datetime.now(UTC).isoformat()
    output_path.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "result": "passed",
                "report": str(output_path),
                "workloads": len(workloads),
                "concurrent_workloads": len(concurrent_workloads),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
