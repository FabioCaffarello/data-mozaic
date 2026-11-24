# Execution Coordination v0

This package hosts BC04's engine-independent Run/Attempt state and application coordination plus a local composition adapter. The domain imports only its own types and ports. The Scrapy container adapter is the technical BC05 execution adapter in this fixture-only composition; it invokes the prebuilt Scrapy Collector through Docker CLI and inspects the effective container before start.

The in-memory repository is atomic only inside one process and loses all state if that process exits. There are no retries, durable recovery, distributed leases, BC11 admission, BC06 custody, or productive egress. A successful adapter output stays in private staging until BC04 accepts success; failed, late, canceled, or timed-out outputs are discarded. Each container receives a private per-Attempt bind mount for output; the worker and adapter enforce byte/record budgets, but this slice does not apply a kernel filesystem quota to that host directory.

Run local tests with make execution-coordination-tests. make execution-coordination-protected-e2e is a Linux/Docker acceptance target and is required in Integration Security Gate. Direct host execution of Scrapy is not a protected operational path.
