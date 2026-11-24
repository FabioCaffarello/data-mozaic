"""Reproducible synthetic BC05 MongoDB staging/seal/query measurements."""

from __future__ import annotations

import argparse
import json
import os
import platform
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from acquisition_output.adapters.mongodb import (
    MongoOutputStore,
    MongoResources,
    MongoSettings,
)
from acquisition_output.adapters.mongodb.config import MongoProvisioningSettings
from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import (
    OutputIdentity,
    OutputSetBinding,
    OutputSetSpec,
    PayloadContractRef,
    ProducerIdentity,
    RecordOutput,
    ScopedOperationIdentity,
    SourceRevisionRef,
    canonical_digest,
)
from pymongo import MongoClient


class _Validator:
    def validate(self, contract, payload):
        if contract.contract_id != "benchmark.synthetic.v1":
            raise ValueError("unexpected benchmark contract")


class _NoCustody:
    def confirm(self, artifact, *, tenant_id, source):
        raise AssertionError("record benchmark should not request custody")


def percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)

    def at(fraction: float) -> float:
        return (
            ordered[min(len(ordered) - 1, round((len(ordered) - 1) * fraction))] * 1000
        )

    return {"p50_ms": at(0.50), "p95_ms": at(0.95), "p99_ms": at(0.99)}


def build_app(resources: MongoResources):
    store = MongoOutputStore(resources)
    return AcquisitionOutputApplication(
        store, store, payload_contracts=_Validator(), custody=_NoCustody(), outbox=store
    )


def new_set(app, tenant, set_id, contract):
    binding = OutputSetBinding(
        tenant,
        SourceRevisionRef("benchmark-fixture", "synthetic-v1"),
        ProducerIdentity("bc05-benchmark", "1"),
    )
    app.create_output_set(
        OutputSetSpec(set_id, binding, (contract,)),
        ScopedOperationIdentity(tenant, f"create-{set_id}"),
    )
    return binding


def output_for(binding, set_id, contract, ordinal):
    output_id = f"output-{ordinal:08d}"
    return RecordOutput(
        identity=OutputIdentity(output_id, f"obs-{ordinal:08d}", datetime.now(UTC)),
        operation=ScopedOperationIdentity(
            binding.tenant_id, f"stage-{set_id}-{ordinal:08d}"
        ),
        output_set_id=set_id,
        binding=binding,
        payload={"ordinal": ordinal, "label": f"synthetic-record-{ordinal}"},
        payload_contract=contract,
    )


def server_transaction_stats(client):
    value = client.admin.command("serverStatus").get("transactions", {})
    keys = (
        "totalStarted",
        "totalCommitted",
        "totalAborted",
        "currentActive",
        "retriedCommandsCount",
        "retriedStatementsCount",
    )
    return {key: value[key] for key in keys if key in value}


def db_sizes(database):
    result = database.command("dbStats", scale=1)
    output_stats = database.command("collStats", "capture_outputs", scale=1)
    return {
        "storage_size_bytes": result.get("storageSize", 0),
        "index_size_bytes": result.get("totalIndexSize", 0),
        "output_documents": database.capture_outputs.count_documents({}),
        "capture_outputs_storage_bytes": output_stats.get("storageSize", 0),
        "capture_outputs_index_bytes": output_stats.get("totalIndexSize", 0),
        "capture_outputs_logical_bytes": output_stats.get("size", 0),
    }


def sequential_case(resources, metrics_client, count):
    app = build_app(resources)
    tenant = "bench-" + uuid4().hex
    set_id = "seq-" + uuid4().hex
    contract = PayloadContractRef(
        "benchmark.synthetic.v1", "1.0.0", canonical_digest({"benchmark": 1})
    )
    binding = new_set(app, tenant, set_id, contract)
    before_sizes = db_sizes(resources.database)
    before_tx = server_transaction_stats(metrics_client)
    tracemalloc.start()
    samples = []
    started = time.perf_counter()
    for ordinal in range(count):
        output = output_for(binding, set_id, contract, ordinal)
        tick = time.perf_counter()
        app.stage_output(output)
        samples.append(time.perf_counter() - tick)
    staged_seconds = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    tick = time.perf_counter()
    sealed = app.seal_output_set(
        tenant, set_id, ScopedOperationIdentity(tenant, f"seal-{set_id}")
    )
    seal_seconds = time.perf_counter() - tick
    app.publish_output_set(
        tenant,
        set_id,
        sealed.manifest_digest,
        ScopedOperationIdentity(tenant, f"publish-{set_id}"),
    )
    query_start = time.perf_counter()
    cursor = None
    read_count = 0
    while True:
        page = app.read_published_outputs(tenant, set_id, cursor=cursor, limit=500)
        read_count += len(page.outputs)
        cursor = page.next_cursor
        if cursor is None:
            break
    query_seconds = time.perf_counter() - query_start
    after_sizes = db_sizes(resources.database)
    after_tx = server_transaction_stats(metrics_client)
    return {
        "count": count,
        "stage_throughput_outputs_per_second": count / staged_seconds,
        "stage_latency": percentiles(samples),
        "stage_python_tracemalloc_peak_bytes": peak_bytes,
        "seal_seconds": seal_seconds,
        "pagination_outputs_per_second": read_count / query_seconds
        if query_seconds
        else None,
        "pagination_seconds": query_seconds,
        "read_count": read_count,
        "database_growth": {
            key: after_sizes[key] - before_sizes[key] for key in before_sizes
        },
        "transaction_counters_delta": {
            key: after_tx.get(key, 0) - before_tx.get(key, 0) for key in after_tx
        },
    }


def concurrent_case(resources, *, outputs_per_set: int, independent_sets: bool):
    worker_count = 4
    suffix = uuid4().hex
    contract = PayloadContractRef(
        "benchmark.synthetic.v1", "1.0.0", canonical_digest({"benchmark": 1})
    )
    tenants = [f"bench-{suffix}-{index}" for index in range(worker_count)]
    set_ids = [
        f"con-{suffix}-{index if independent_sets else 'shared'}"
        for index in range(worker_count)
    ]
    primary_app = build_app(resources)
    bindings = [
        new_set(primary_app, tenants[index], set_ids[index], contract)
        for index in range(worker_count if independent_sets else 1)
    ]
    if not independent_sets:
        bindings *= worker_count
        set_ids *= worker_count
        tenants *= worker_count
    sample_sets: list[list[float]] = [[] for _ in range(worker_count)]

    def run_worker(index):
        with MongoResources(MongoSettings.from_env()) as worker_resources:
            app = build_app(worker_resources)
            for ordinal in range(outputs_per_set):
                output = output_for(
                    bindings[index],
                    set_ids[index],
                    contract,
                    index * outputs_per_set + ordinal,
                )
                tick = time.perf_counter()
                app.stage_output(output)
                sample_sets[index].append(time.perf_counter() - tick)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        list(pool.map(run_worker, range(worker_count)))
    elapsed = time.perf_counter() - started
    return {
        "workers": worker_count,
        "outputs_per_worker": outputs_per_set,
        "independent_sets": independent_sets,
        "total_outputs": worker_count * outputs_per_set,
        "throughput_outputs_per_second": worker_count * outputs_per_set / elapsed,
        "latency": percentiles([latency for group in sample_sets for latency in group]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--counts", default="100,10000")
    parser.add_argument("--include-100k", action="store_true")
    args = parser.parse_args()
    counts = [int(value) for value in args.counts.split(",") if value]
    if args.include_100k:
        counts.append(100_000)
    if any(count < 1 or count > 100_000 for count in counts):
        raise SystemExit("benchmark counts must be between 1 and 100000")
    results = {
        "environment": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "mongo_server": "MongoDB 8.0.38 single-node replica set; 2 CPU/2 GiB container limits",
            "driver": "PyMongo 4.20.0",
            "chunk_size": 1,
            "output_set_limit": 10000,
        },
        "sequential": [],
        "concurrent_same_set": [],
        "concurrent_independent_sets": [],
    }
    admin_settings = MongoProvisioningSettings.from_env()
    with (
        MongoClient(
            admin_settings.uri, serverSelectionTimeoutMS=5000
        ) as metrics_client,
        MongoResources(MongoSettings.from_env()) as resources,
    ):
        for count in counts:
            if count <= 10_000:
                results["sequential"].append(
                    sequential_case(resources, metrics_client, count)
                )
            else:
                # The domain limit is 10,000 per set; this workload partitions 100k
                # across ten independent sets without weakening that bound.
                per_set = 10_000
                started = time.perf_counter()
                subresults = [
                    sequential_case(resources, metrics_client, per_set)
                    for _ in range(count // per_set)
                ]
                results["sequential"].append(
                    {
                        "count": count,
                        "partition_count": len(subresults),
                        "elapsed_seconds": time.perf_counter() - started,
                        "sets": subresults,
                    }
                )
        results["concurrent_same_set"].append(
            concurrent_case(resources, outputs_per_set=100, independent_sets=False)
        )
        results["concurrent_independent_sets"].append(
            concurrent_case(resources, outputs_per_set=100, independent_sets=True)
        )
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
