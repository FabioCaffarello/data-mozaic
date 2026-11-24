# ADR 021 — BC04 fixture execution coordination and protected Scrapy adapter

- Status: Proposed
- Owners: BC04 Execution Coordination; technical execution adapter ownership in BC05; platform security for container policy
- Scope: one-process fixture coordination using the installed Scrapy Collector image; no production acquisition or distributed coordination

## Context

PR #13 installed a multi-bot Scrapy project and exact BotBuild selector, but there was no owner for CaptureRun/Attempt state and no protected invocation path from a coordinator. A first integration should prove identity binding, lifecycle, cancellation, terminal outcomes and output control without placing Scrapy or Docker in the domain.

## Decision proposed

1. BC04 owns CaptureRun, one CaptureAttempt per Run in this slice, the state machine, scoped idempotency, deadline, cancellation intent and the terminal business decision.
2. The first implementation is an application module in apps/execution-coordination, not a separate service per bounded context. Its domain depends only on its own types; the BC04 ExecutionPort is consumed by the local composition.
3. A technical BC05 adapter in that same composition invokes only an immutable Scrapy Collector image through the Docker CLI. The request cannot choose an image, command, settings, module, URL, path, credentials or sandbox flags. Trusted configuration binds exact BotBuild, catalog, assembly, image and fixture identities.
4. ExecutionRequest v0 and ExecutionReport v0 are internal draft JSON contracts because the request crosses the host/container boundary. The worker consumes a read-only request file and verifies its catalog/build/source/fixture binding before selecting a BotBuild.
5. The adapter executes create -> inspect -> start. It rejects a container unless effective image ID, entrypoint, network none, non-root UID, read-only root, exact mounts, CPU/memory/PID limits, dropped capabilities and no-new-privileges match trusted configuration.
6. A report is reconciled under the same coordinator lock that serializes cancellation. Successful output remains private staging until BC04 accepts success; a winning cancellation, timeout, failure, late result or rejected report discards staging.
7. The initial repository is in-memory and atomic only in one coordinator process. No durable recovery, retry, exactly-once, leases, distributed scheduler, BC11 production authorization, BC06 custody or productive egress is claimed.
8. The OCI manifest digest and Docker local image ID are distinct identities. The trusted build pipeline associates them; the daemon executes by the exact local image ID and checks an OCI repo digest when the daemon exposes one. This local association is not a publisher signature or release approval.

## State and race rules

Run transitions are accepted -> launching -> running -> terminal; accepted or launching can also be cancelled/timed out/failed before running. running -> cancel_requested -> cancelled prevents a late success from winning. If the deadline has elapsed when cancellation is requested, timeout wins; once cancellation intent is accepted first, it wins over a later timeout or report. Otherwise, the first terminal transition accepted atomically by the repository wins. Report acceptance and cancellation are additionally serialized by the coordinator lock. A worker/container crash is a technical report status (`crashed`) and a BC04 domain outcome of `failed`, with diagnostic `WORKER_CRASHED`; crash is not a separate Run/Attempt state. A terminal Run never reopens. Identical terminal reports are idempotent; conflicting or mismatched reports are rejected. There are no automatic retries.

Run deadline is absolute. Worker execution duration is capped by trusted BotBuild/profile configuration and further bounded by remaining Run time. A container stop has a short grace period followed by forced kill. Startup Docker commands are individually bounded by the remaining deadline.

## Alternatives

- BC04 port plus local protected container adapter (selected): keeps domain engine-independent, proves the real container boundary, and has one host-controlled composition.
- Adapter directly in the Scrapy worker: rejected because the worker would own Run lifecycle/policy and coordinate itself, and the coordinator could not supervise cancellation independently.
- Independent remote BC05 service: deferred; no remote consumer, deployment, durable queue or scaling requirement justifies the network/API and recovery surface.
- PostgreSQL immediately: deferred; an in-memory repository tests domain transitions but explicitly provides no restart recovery. Add durable persistence only with a requirement for coordinator restart/concurrent ownership.

## Consequences

The local Docker daemon is a privileged trust boundary: a compromised coordinator/adapter can control that daemon. No Docker socket is mounted into the worker. A coordinator crash can leave an orphan container/output because no durable registry/reconciler exists. The E2E harness scopes cleanup to container IDs created by that run; it does not prune unrelated resources. Output uses a private per-Attempt host bind mount so the adapter can recover it after container exit. Worker and adapter enforce total byte and record budgets; the container also receives a per-file `fsize` limit. There is no aggregate kernel filesystem quota on the host bind mount, so bounded concurrency and generated/runtime output budgets remain necessary residual controls. Temporary outputs are not BC06 RawArtifactRefs.

Playwright can later implement the same execution port only if it shares Run-level identity, outcome and budget semantics. Browser-specific execution/capability data stays in an engine-specific adapter and report extension with an actual consumer; no engine plugin framework is introduced.

## Validation criteria

- Domain tests cover allowed/prohibited transitions, same-key replay/conflict, duplicate/conflicting reports and cancellation/timeout races.
- Contract consumer tests prove closed request/report schemas and identity binding.
- Linux E2E runs HTML and JSON through the real Scrapy image, then real callback failure, timeout, cancel and container kill; it verifies no failed output publication, a healthy subsequent run, exact container sandbox settings, network-denial probe and compiler absence.
- CI must run the protected E2E as a required Integration Security Gate step.
- Restart durability, production authorization/custody, publisher trust, egress and distributed scheduling remain separate review gates.
