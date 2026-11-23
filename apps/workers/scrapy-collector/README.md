# Scrapy Collector

`mozaic-scrapy-collector` is one installable Scrapy project that assembles multiple immutable BC15 BotBuilds. It resolves each execution by the exact `(bot_id, build_id)` pair and starts a fresh Python process for the selected build.

The initial profile is fixture-only. The worker does not parse CapturePlaybooks, compile IR, authorize source access, or download code dynamically. BotBuilds must be produced and reviewed before assembly.

The CLI starts a fresh Python child process, but that alone is not an OS sandbox. The protected Linux conformance path is `make scrapy-collector-multibot-conformance`; it runs one selected build per container with `--network none`, a non-root UID, read-only root filesystem, resource limits, and a private output volume for each execution. There is not yet a supported production container launcher or authorization integration. Do not run untrusted BotBuild code directly on a host or in a container with default networking and shared writable output mounts.

## Commands

```sh
scrapy-collector catalog list
scrapy-collector run --bot-id BOT_ID --build-id BUILD_ID
scrapy-collector assemble --manifest bot-catalog/manifest.yaml \
  --build-root PATH --fixture-root PATH --output PATH --lockfile uv.lock \
  --target-platform linux/amd64
```

`assemble` is a build-time command. Runtime `run` accepts IDs only; catalog and output roots are fixed by deployment configuration. The fixture tree is copied and verified during assembly.

The repository target `make scrapy-collector-multibot-conformance` builds HTML and JSON BotBuilds plus a second source revision, assembles them, and exercises isolated executions. Linux CI runs that target inside the non-root, network-disabled security gate.

Compilation records both the compiler host toolchain and an explicit runtime target such as `linux/amd64`. Assembly rejects mismatched build targets, Python minor versions, Scrapy/runtime compatibility, or lockfile identity. The initial image supports Linux/amd64 only.

See the proposed decision in [ADR 020](../../../docs/architecture/decisions/020-scrapy-collector-multi-bot-project.md) and the [BotBuild manifest v2 draft](../../../contracts/bot-build/draft/v2/README.md).
