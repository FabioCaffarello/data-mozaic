# Scout trusted MCP broker deployment

**State: implementation and install bundle preparation exist; trusted host deployment is not verified.** ADR 017 remains Proposed. Do not enable production fixture use until an operator provisions the package and proves managed MCP policy and process isolation.

## HTTP transport and protocol

The existing low-level MCP `Server` and tool handlers are reused through the SDK `StreamableHTTPSessionManager`; there is one business pipeline. The service entrypoint is `python -m scout.tool_host`. It binds only `127.0.0.1:8765`, serves `/mcp`, and has an authenticated `/healthz`. The HTTP adapter requires a bearer token, exact `Host: 127.0.0.1:8765`, and either no `Origin` or `http://127.0.0.1:8765`; the SDK DNS-rebinding checks are also enabled. Requests are capped at 1 MB, active HTTP requests at eight, sessions at 32, and idle sessions at 15 minutes. An absent token, invalid startup config, unavailable database, invalid policy, Docker endpoint mismatch, or missing worker image terminates startup; there is no STDIO or direct replay fallback.

The repository locks MCP Python SDK 1.30.0. Its HTTP session manager negotiates the protocol version during `initialize`; the SDK advertises `2024-11-05`, `2025-03-26`, `2025-06-18`, and `2025-11-25`. The deterministic client test negotiates through this SDK. The Codex CLI 0.170.0 handshake against an installed service still needs operator-run verification. The Streamable HTTP transport requires one endpoint supporting POST/GET and `Origin` validation; local servers should bind loopback and authenticate requests ([MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)).

## Build and verify a Linux release bundle

Protected deployment targets Linux first. Build natively on a clean Linux runner for each architecture; do not use a macOS-generated wheelhouse. CI currently targets `ubuntu-24.04` / `x86_64` and publishes `linux/amd64`. `linux/arm64` requires a separate native ARM64 runner and a separate bundle. macOS is a development/test host, not a supported protected deployment target.

The worker build and image import must run only against a disposable CI/release Docker daemon. Never point these commands at a user's productive Docker Desktop daemon. The worker builder requires an immutable base image reference:

```sh
uv run --locked python tooling/build_bc15_worker_image.py \
  --base-image 'python@sha256:<reviewed-base-digest>' \
  --platform linux/amd64 \
  --docker-host 'unix:///var/run/docker.sock' \
  --verify-docker-host 'unix:///var/run/docker.sock' \
  --output /release/bc15-worker
uv run --locked python tooling/package_scout_broker.py \
  --target-platform linux/amd64 \
  --worker-bundle /release/bc15-worker \
  --output /release/scout-broker
uv run --locked python tooling/verify_scout_bundle.py /release/scout-broker \
  --expected-commit '<full-reviewed-commit>' \
  --expected-tree '<reviewed-git-tree>' \
  --target-platform linux/amd64 \
  --expected-manifest-sha256 '<digest-from-independent-release-record>' \
  --source-root "$PWD" \
  --docker-host 'unix:///var/run/docker.sock'
sha256sum /release/scout-broker/manifest.json
```

The builders reject dirty or untracked worktrees. The worker builder declares the full source commit/tree, Dockerfile and input hashes, digest-pinned base reference, explicit OS/architecture, Docker image ID (the content digest of the image config), exported archive SHA-256, archive config digest, rootfs diff IDs, runtime config, and ID/platform/config after `docker load`. It removes only the unique image it just built from the explicitly selected disposable daemon before importing the archive, so a successful build proves an actual import round trip. `RepoDigest` is recorded separately; it is not the image ID or archive hash. The verifier independently parses the archive's `manifest.json` and config blob, checks the blob hash and filename, compares its OS/architecture and entrypoint/config with the manifest, and can inspect the imported image in the selected daemon.

The Linux bundle contains broker/observability wheels, a hash-locked requirements export and Linux-targeted wheelhouse, the exported worker image, `uv.lock`, manifests and helper scripts. The package build refuses a non-Linux host or a target/platform mismatch. The verifier requires expected commit, Git tree, target platform, and (for an operator release) a manifest digest obtained independently. `--source-root` adds a clean-checkout/source-input comparison; `--docker-host` checks the image imported in that disposable daemon. In-bundle hashes alone are not authentication: the verifier cannot prove its own origin if both it and the package are replaced. The workflow artifact and immutable workflow revision are distribution evidence, not a cryptographic signature or independent attestation. The operator must compare source SHA/tree and manifest SHA against a trusted GitHub workflow record before installation. Never treat a prior macOS or pre-commit bundle as a Linux release.

The Linux CI job verifies both the prepared directory and a fresh download of the uploaded artifact against the clean source checkout and the pre-upload manifest digest. This ensures the bytes delivered by Actions are the same complete bundle the job validated. The packager removes the specific non-package `wheels/.gitignore` generated by `uv build`; unexpected hidden wheel output is rejected rather than relying on artifact-upload settings that omit hidden files.

`install_scout_bundle.sh BUNDLE PREFIX EXPECTED_COMMIT EXPECTED_TREE TARGET EXPECTED_MANIFEST_SHA256 DOCKER_HOST` verifies the bundle and imported image before installing only into a new unprivileged staging directory. It does not use sudo, load an image, create a service, or write administrative policy. The operator separately loads the archive into the intended protected daemon, verifies the exact config-derived image ID and target platform, then provisions the service. The broker never builds an image; it accepts only the operator-configured image ID (`sha256:…`) and fixed context/endpoint, then verifies that image before exposing protected operations.

## Operator provisioning

### Linux with native Docker

1. Install the verified bundle under a root-owned versioned path such as `/opt/data-mozaic/scout-broker/<version>`. Make release files non-writable by both the broker and Codex identities. Give a dedicated `scout-broker` service UID read/execute access to the venv and write access only to its `/var/lib/data-mozaic/scout` state, evidence, and artifact roots.
2. Load the reviewed worker archive through the intended Docker daemon. Recompute the SHA-256 of the config blob named in archive `manifest.json`; it must equal `worker-manifest.json:worker_image_id`, which must also equal `archive_config_digest` and the imported Docker `.Id`. Verify `.Os=linux`, `.Architecture` matches the target, and compare entrypoint, command, user, working directory, and rootfs diff IDs. Configure `SCOUT_BC15_WORKER_IMAGE_DIGEST`, `SCOUT_DOCKER_EXECUTABLE`, `SCOUT_DOCKER_CONTEXT`, and `SCOUT_DOCKER_ENDPOINT` in a root-owned service environment file. Do not use a mutable tag as image identity.
3. Grant the broker service identity access to the Docker socket. Membership in the `docker` group is effectively root-equivalent; Codex must use another UID, must not be in that group, and must fail an OS-level socket connect probe. The daemon and all service files/configuration need independent root ownership.
4. Resolve the Docker CLI path as the root-owned executable's real path. Check the file and every parent directory are root-owned and not group/world writable; record symlink resolution, mode, inode/hash and version. The broker validates these conditions at startup and invokes that absolute path with a fixed PATH and no shell. Ensure the service PATH cannot shadow it. Keep Docker context config/service environment root-owned, pin the Unix endpoint, and omit `DOCKER_HOST`, `DOCKER_CONTEXT`, `PYTHONPATH`, and workspace-derived paths from the service environment.
5. Use a root-owned systemd unit with `User=scout-broker`, `WorkingDirectory=/`, `ExecStart=/opt/.../venv/bin/python -I -m scout.tool_host`, a protected environment file, restart-on-failure, and write paths limited to declared state/data roots. Keep release code and venv read-only to the service. Set `PYTHONDONTWRITEBYTECODE=1`. Verify the running process's executable, argv, environment allowlist, working directory, module origins and ownership after every restart.

### macOS with Docker Desktop

The protected deployment profile in this release is Linux only. macOS is not accepted as a protected deployment based on code-level checks or Docker Desktop volume placement.

1. Install the same reviewed files under a root-owned path such as `/Library/Application Support/Data Mozaic/scout-broker/<version>`; the LaunchAgent plist and environment/configuration must also be root-managed and non-writable by the workspace user.
2. Docker Desktop exposes its daemon through a logged-in user's socket/context. There is no separate daemon UID equivalent to native Linux in this setup. The broker may need to run as the desktop user, so same-UID filesystem mode bits do not isolate it from Codex. This platform is unsupported unless the effective Codex shell sandbox denies every Docker endpoint and an enforced managed MCP policy prevents project config from starting any unapproved STDIO process. Verify actual process identity, socket paths, and access after service and Codex restart.
3. Fix the Docker context and endpoint from operator-owned config. For example, Docker Desktop may use `desktop-linux` and a user socket under `~/.docker/run`; do not infer the endpoint from this example. The broker verifies the context's inspected endpoint against the configured Unix socket and rejects non-Unix endpoints.

## Rollback

Keep the prior root-owned release directory and worker image until the new version passes its operator checks. To roll back, stop the service (`systemctl stop ...` on Linux or the operator-managed `launchctl` job on macOS), restore the prior versioned executable/config reference and matching worker image ID, then restart and check authenticated `/healthz` and the service log. Keep the SQLite registry and labeled volumes intact; do not run global Docker cleanup or remove volumes during rollback. This change currently keeps registry schema version `1`; if a future release changes it incompatibly, stop and take an operator-controlled SQLite backup before upgrade, and use that backup only with the matching prior binary. Confirm MCP tool availability and policy again after Codex restart.

For both platforms, configure owner-only absolute paths for the policy, local evidence, artifact, acceptance-oracle and SQLite files. Set the PostgreSQL DSN and bearer token through the service manager's protected secret channel, never command arguments, project files, MCP parameters, or logs. The MCP bearer token authenticates only the fixed broker API; it grants no Docker API access by itself. The project Codex config resolves it from `SCOUT_BROKER_TOKEN`, so that variable is present in the Codex process.

**Codex CLI 0.170.0 defaults shell subprocesses to inheriting the full parent environment and sets `ignore_default_excludes=true`; variables named `*_TOKEN` are therefore not implicitly removed.** The project config now excludes `SCOUT_BROKER_TOKEN` as a convenience, but the agent can edit that file. The exact filter is also in the managed requirements example and must be enforced by the administrator:

```toml
[shell_environment_policy]
inherit = "all"
ignore_default_excludes = true

[shell_environment_policy.filters]
SCOUT_BROKER_TOKEN = "exclude"
```

The Codex-provided macOS sandbox test with a synthetic token confirmed the filtered child environment omitted the variable; a `ps` process-inspection attempt was denied in that sandbox. This does not prove an active managed policy or every same-UID process-inspection path. Verify the effective managed filter and repeat the synthetic probe after provisioning/restart. If the shell can obtain the token, or the filter is not present in administrator-managed requirements, do not enable the broker. The CLI's 0.170.0 defaults are visible in the [versioned Codex source](https://github.com/openai/codex/blob/rust-v0.170.0/codex-rs/protocol/src/config_types.rs).

## Codex administrative requirements

The sample [codex-requirements.example.toml](../../config/scout/codex-requirements.example.toml) identifies the exact server name and URL and excludes the broker token from shell subprocesses. In Codex CLI 0.170.0, an active non-empty `mcp_servers` requirements map disables direct configured servers whose name or transport identity does not match. Requirements' keyed shell environment filters can enforce token exclusion; project-local configuration cannot serve as that authority. Plugin-provided MCP servers need their own managed plugin requirements or must be disabled; do not assume the direct MCP map covers plugin servers. Do not add wildcards.

- Linux/Unix: an administrator installs `/etc/codex/requirements.toml` (administrator-owned, not writable by Codex). The CLI also recognizes legacy `/etc/codex/managed_config.toml`.
- macOS: use only a forced MDM preference `com.openai.codex:requirements_toml_base64`; an ordinary user preference is not administrative enforcement.
- Enterprise cloud requirements may add a managed layer; the policy source and resulting effective values must be checked by the administrator.

The repository example is not active policy. This host has not had an enforced policy provisioned or confirmed. Do not modify `/etc/codex`, MDM, or user-wide configuration as part of this task.

After operator provisioning, run `tooling/probe_codex_mcp_allowlist.py` only under a dedicated authenticated `SCOUT_CODEX_TEST_HOME`, with `SCOUT_POLICY_EVIDENCE_DIR` pointing to a private operator evidence directory and a synthetic `SCOUT_BROKER_TOKEN`. It creates disposable MCP STDIO and HTTP servers, starts actual `codex exec --json --ephemeral` sessions with the supported `workspace-write` sandbox, rewrites project configuration between fresh Codex processes, and checks whether the synthetic STDIO process or alternate HTTP endpoint was reached. Each session must also complete a harmless `scout_preflight` call against a deliberately nonexistent source through the approved HTTP broker; this proves the Codex session initialized and invoked that MCP identity without using the fixture. Do not approve any elevation or shell operation; if the session cannot complete without one, classify it as inconclusive. The JSON session must contain a completed thread/turn and the expected MCP tool call; a missing marker without those events is inconclusive. Preserve the `codex mcp list --json` output, session events, policy source/provenance, exact managed-policy hash and Codex version. The script does not claim a pass from marker absence alone. Run separate disposable VM snapshots with managed policy absent and invalid as negative controls: the STDIO startup marker or alternate HTTP request must be observed, otherwise the test did not exercise the control. No host policy test was run here; the managed policy remains pending.

Record the effective Codex policy from its administrative source, not the repository TOML. Repeat after a Codex restart and adversarial project-config rewrite. If startup attempts, policy provenance, disabled reasons, or negative controls are missing, classify enforcement as **NOT VERIFIED**. Do not change `/etc/codex`, MDM, or administrative settings as part of this release task.

## Required deployment evidence

Retain release/source hashes, worker image ID, package manifest SHA, service UID, exact executable and loaded module paths, protected config ownership/modes, Docker context/endpoint, socket denial from the Codex process, effective Codex requirements source, and reports from both service and Codex restarts. Verify unauthenticated, revoked/cross-session, invalid Host/Origin, oversized, malformed, concurrent, wrong source/revision, and disallowed tool requests. Confirm the broker process imports only the installed wheel and never accesses the checkout. The deterministic E2E now exercises HTTP MCP → broker → BC15 image → replay/acceptance, but it is not the deployment proof.

The direct BC15 writer remains a trusted in-process API and does not defend against a same-UID process moving its output directory. Only the protected broker/container composition is the supported untrusted-agent path. The worker has no network or host bind mounts; volumes remain daemon-managed and are removed only after ownership-label verification. Docker administrators, daemon compromise, container escape, and disk exhaustion beyond configured run-count/retention limits remain out of scope.

The real synthetic Codex fixture approval is expired. Do not start a real Codex fixture session until the owner explicitly grants a valid one-use approval.

For the future one-use request, ask the owner to authorize only `e2e-catalog-single` / `single-resource-v1`: one HTTPS `GET` to `https://catalog.fixture.test/api/catalog`, at most one request and one page, at most 4096 response bytes, 120 seconds maximum session duration, one execution only, and approval expiry no later than 30 minutes after grant. Deny all other sources and public/external destinations. Do not write this approval into policy or start a session until the owner explicitly grants it.

## Disk and capacity operations

Docker named volumes do not have a physical disk quota in this implementation. Run count, concurrency, worker memory/CPU/PID limits, body limits, and 24-hour retention constrain application behavior but do not cap Docker Desktop VM or Linux filesystem consumption. Monitor the Docker data root/VM free space and volume growth. The operator must define and alert on a minimum free-space threshold before enabling the broker; stop accepting new replay/acceptance work below it, preserve in-retention evidence, and investigate/clean only volumes the broker's ownership labels and registry prove it owns. Never use global `docker system prune` or `volume prune` for this service.
