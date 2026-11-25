"""Bounded synthetic BC06 transfer benchmark with service-container sampling."""

from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import json
import os
import platform
import re
import resource
import subprocess
import tempfile
import threading
import time
import tracemalloc
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

BASE_URL = os.environ.get("ARTIFACT_CUSTODY_HTTP_URL", "http://127.0.0.1:8086")
TENANT = "tenant-benchmark-synthetic"
SOURCE = {"source_id": "synthetic-benchmark", "revision": "local-v1"}
CHUNK = 64 * 1024


def make_file(path: Path, size: int) -> str:
    block = hashlib.sha256(b"data-mozaic-bc06-synthetic-benchmark-v1").digest() * 2048
    digest = hashlib.sha256()
    remaining = size
    with path.open("wb") as stream:
        while remaining:
            part = block[: min(len(block), remaining)]
            stream.write(part)
            digest.update(part)
            remaining -= len(part)
    return "sha256:" + digest.hexdigest()


def docker_container_id() -> str:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "label=com.docker.compose.project=data-mozaic-bc06-local",
            "--filter",
            "label=com.docker.compose.service=artifact-custody",
            "--format",
            "{{.ID}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip().splitlines()
    if len(value) != 1:
        raise RuntimeError("expected exactly one local BC06 service container")
    return value[0]


def bytes_from_docker(value: str) -> int:
    match = re.search(r"([0-9.]+)\s*(B|KiB|MiB|GiB)", value)
    if not match:
        raise ValueError(f"unrecognized Docker memory quantity: {value!r}")
    scale = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
    return int(float(match.group(1)) * scale[match.group(2)])


class ServiceMemorySampler:
    def __init__(self, container_id: str) -> None:
        self.container_id = container_id
        self.stop = threading.Event()
        self.peak_bytes = 0
        self.samples = 0
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def _sample(self) -> None:
        while not self.stop.is_set():
            result = subprocess.run(
                [
                    "docker",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{.MemUsage}}",
                    self.container_id,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                try:
                    value = bytes_from_docker(result.stdout.split("/", 1)[0].strip())
                    self.peak_bytes = max(self.peak_bytes, value)
                    self.samples += 1
                except (ValueError, IndexError):
                    pass
            self.stop.wait(0.25)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.stop.set()
        self.thread.join(timeout=3)


def run_transfer(size_mib: int, directory: str, token: str) -> dict[str, object]:
    size = size_mib * 1024 * 1024
    suffix = uuid.uuid4().hex
    operation_id, artifact_id = "bench-op-" + suffix, "bench-artifact-" + suffix
    path = Path(directory) / (artifact_id + ".bin")
    digest = make_file(path, size)
    intent = {
        "contract_version": "artifact-custody.v1",
        "operation_id": operation_id,
        "tenant_id": TENANT,
        "artifact_id": artifact_id,
        "source": SOURCE,
        "artifact": {
            "artifact_id": artifact_id,
            "digest": digest,
            "size_bytes": size,
            "media_type": "application/octet-stream",
        },
        "representation": "downloaded_file_bytes",
        "classification": "internal",
        "retention_policy_ref": "synthetic-benchmark-v1",
    }
    headers = {"Authorization": f"Bearer {token}", "X-DM-Tenant-ID": TENANT}
    request = Request(
        BASE_URL.rstrip("/") + "/draft/v1/artifact-intents",
        data=json.dumps(intent, separators=(",", ":")).encode(),
        headers=headers,
        method="POST",
    )
    start = time.perf_counter()
    with urlopen(request, timeout=30) as response:
        response.read()
    intent_seconds = time.perf_counter() - start

    host = BASE_URL.removeprefix("http://").removeprefix("https://").split("/", 1)[0]
    hostname, _, port = host.partition(":")
    connection = http.client.HTTPConnection(hostname, int(port or 80), timeout=900)
    start = time.perf_counter()
    connection.putrequest("PUT", f"/draft/v1/artifact-intents/{operation_id}/content")
    for key, value in headers.items():
        connection.putheader(key, value)
    connection.putheader("Content-Type", "application/octet-stream")
    connection.putheader("Content-Length", str(size))
    connection.endheaders()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            connection.send(chunk)
    upload_response = connection.getresponse()
    upload_body = upload_response.read()
    upload_seconds = time.perf_counter() - start
    if upload_response.status != 200:
        raise RuntimeError(
            f"synthetic upload failed: HTTP {upload_response.status} {upload_body[:200]!r}"
        )
    connection.close()

    connection = http.client.HTTPConnection(hostname, int(port or 80), timeout=900)
    start = time.perf_counter()
    connection.request(
        "GET", f"/draft/v1/artifacts/{artifact_id}/content", headers=headers
    )
    response = connection.getresponse()
    check = hashlib.sha256()
    read_bytes = 0
    while chunk := response.read(CHUNK):
        check.update(chunk)
        read_bytes += len(chunk)
    download_seconds = time.perf_counter() - start
    if (
        response.status != 200
        or read_bytes != size
        or "sha256:" + check.hexdigest() != digest
    ):
        raise RuntimeError(
            "synthetic download did not preserve the declared byte digest"
        )
    connection.close()
    return {
        "size_mib": size_mib,
        "bytes": size,
        "intent_seconds": round(intent_seconds, 4),
        "upload_seconds": round(upload_seconds, 4),
        "upload_mib_s": round(size_mib / upload_seconds, 2),
        "download_seconds": round(download_seconds, 4),
        "download_mib_s": round(size_mib / download_seconds, 2),
        "sha256_verified": True,
    }


def main() -> None:
    token = os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"]
    sizes = [
        int(item)
        for item in os.environ.get("BC06_BENCH_SIZES_MIB", "1,10,100").split(",")
    ]
    samples = int(os.environ.get("BC06_BENCH_SAMPLES", "2"))
    concurrent_count = int(os.environ.get("BC06_BENCH_CONCURRENCY", "4"))
    if not sizes or any(size < 1 or size > 100 for size in sizes):
        raise SystemExit(
            "BC06 benchmark sizes must be between 1 and the configured 100 MiB limit"
        )
    if not 1 <= samples <= 5 or not 1 <= concurrent_count <= 4:
        raise SystemExit("benchmark sample count must be 1..5 and concurrency 1..4")
    container_id = docker_container_id()
    cases: list[dict[str, object]] = []
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="bc06-benchmark-") as directory:
        with ServiceMemorySampler(container_id) as sampler:
            for size_mib in sizes:
                for sample in range(1, samples + 1):
                    case = run_transfer(size_mib, directory, token)
                    case["sample"] = sample
                    case["workload"] = "sequential"
                    cases.append(case)
            concurrent_size = min(10, max(sizes))
            start = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=concurrent_count
            ) as pool:
                concurrent_cases = list(
                    pool.map(
                        lambda _: run_transfer(concurrent_size, directory, token),
                        range(concurrent_count),
                    )
                )
            concurrent_seconds = time.perf_counter() - start
            cases.append(
                {
                    "workload": "concurrent_upload_download_roundtrips",
                    "concurrency": concurrent_count,
                    "size_mib_each": concurrent_size,
                    "total_mib": concurrent_count * concurrent_size,
                    "wall_seconds": round(concurrent_seconds, 4),
                    "aggregate_mib_s": round(
                        concurrent_count * concurrent_size / concurrent_seconds, 2
                    ),
                    "all_digests_verified": all(
                        case["sha256_verified"] for case in concurrent_cases
                    ),
                }
            )
        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        _, python_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss_bytes = maxrss if platform.system() == "Darwin" else maxrss * 1024
        cases.append(
            {
                "python_peak_rss_bytes": rss_bytes,
                "python_tracemalloc_peak_bytes": python_peak,
                "bc06_container_memory_peak_bytes_sampled": sampler.peak_bytes,
                "bc06_container_memory_samples": sampler.samples,
                "temporary_disk_peak_bytes": "not sampled directly; mount is 420 MiB and process config bounds active spools",
                "platform": platform.platform(),
            }
        )
    print(
        json.dumps(
            {
                "data_classification": "synthetic; not source evidence",
                "endpoint": BASE_URL,
                "chunk_bytes": CHUNK,
                "samples_per_size": samples,
                "container_id": container_id,
                "cases": cases,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
