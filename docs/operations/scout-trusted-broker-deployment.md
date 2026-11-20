# Scout trusted MCP broker deployment

**State: implementation and install bundle preparation exist; trusted host deployment is not verified.** ADR 017 remains Proposed. Do not enable production fixture use until an operator provisions the package and proves managed MCP policy and process isolation.

## HTTP transport and protocol

The existing low-level MCP `Server` and tool handlers are reused through the SDK `StreamableHTTPSessionManager`; there is one business pipeline. The service entrypoint is `python -m scout.tool_host`. It binds only `127.0.0.1:8765`, serves `/mcp`, and has an authenticated `/healthz`. The HTTP adapter requires a bearer token, exact `Host: 127.0.0.1:8765`, and either no `Origin` or `http://127.0.0.1:8765`; the SDK DNS-rebinding checks are also enabled. Requests are capped at 1 MB, active HTTP requests at eight, sessions at 32, and idle sessions at 15 minutes. An absent token, invalid startup config, unavailable database, invalid policy, Docker endpoint mismatch, or missing worker image terminates startup; there is no STDIO or direct replay fallback.

The repository locks MCP Python SDK 1.30.0. Its HTTP session manager negotiates the protocol version during `initialize`; the SDK advertises `2024-11-05`, `2025-03-26`, `2025-06-18`, and `2025-11-25`. The deterministic client test negotiates through this SDK. The Codex CLI 0.170.0 handshake against an installed service still needs operator-run verification. The Streamable HTTP transport requires one endpoint supporting POST/GET and `Origin` validation; local servers should bind loopback and authenticate requests ([MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)).

## Build an install bundle

Build only from a reviewed commit, in a clean release environment and separately for each target OS/CPU. The worker builder requires an immutable base image reference:

```sh
uv run --locked python tooling/build_bc15_worker_image.py \
  --base-image 'python@sha256:<reviewed-base-digest>' \
  --output /release/bc15-worker
uv run --locked python tooling/package_scout_broker.py \
  --worker-bundle /release/bc15-worker \
  --output /release/scout-broker
python3 tooling/verify_scout_bundle.py /release/scout-broker
shasum -a 256 /release/scout-broker/manifest.json
```

The bundle contains broker and observability wheels, a hash-locked dependency file and target-platform wheelhouse, the exported worker image, worker manifest, file hashes, source commit/tree-input digest, lockfile digest, Python/SDK versions, base image reference, and immutable final worker image ID. The build script does not sign the bundle. Its in-bundle hashes detect accidental changes but do not authenticate the bundle if an attacker replaces both manifest and files. The operator must compare the manifest SHA-256 or a release signature through an independent trusted channel before installation. Never install the current dirty-tree build described in this PR as a privileged release.

`install_scout_bundle.sh BUNDLE_DIR STAGING_PREFIX` installs only into a new unprivileged directory. It does not use sudo, load a Docker image, create a service, or write administrative policy. The operator performs those operations only after independent review. The MCP broker never builds an image; it accepts only the operator-configured image ID (`sha256:…`), verifies that exact ID exists in the configured Docker context, and uses it for every worker.

## Operator provisioning

### Linux with native Docker

1. Install the verified bundle under a root-owned versioned path such as `/opt/data-mozaic/scout-broker/<version>`. Make release files non-writable by both the broker and Codex identities. Give a dedicated `scout-broker` service UID read/execute access to the venv and write access only to its `/var/lib/data-mozaic/scout` state, evidence, and artifact roots.
2. Load the reviewed worker archive through the intended Docker daemon. Verify the loaded image ID equals `worker-manifest.json:worker_image_id`. Configure `SCOUT_BC15_WORKER_IMAGE_DIGEST`, `SCOUT_DOCKER_CONTEXT`, and `SCOUT_DOCKER_ENDPOINT` in a root-owned service environment file.
3. Grant the broker service identity access to the Docker socket. Membership in the `docker` group is effectively root-equivalent; Codex must use another UID, must not be in that group, and must fail an OS-level socket connect probe. The daemon and all service files/configuration need independent root ownership.
4. Use a root-owned systemd unit with `User=scout-broker`, `WorkingDirectory=/`, `ExecStart=/opt/.../venv/bin/python -m scout.tool_host`, a protected environment file, restart-on-failure, and write paths limited to the declared state/data roots. Keep the release tree read-only to the service. Set `PYTHONDONTWRITEBYTECODE=1`; do not set `PYTHONPATH`, `DOCKER_CONTEXT`, or workspace-derived paths.

### macOS with Docker Desktop

1. Install the same reviewed files under a root-owned path such as `/Library/Application Support/Data Mozaic/scout-broker/<version>`; the LaunchAgent plist and environment/configuration must also be root-managed and non-writable by the workspace user.
2. Docker Desktop exposes its daemon through a logged-in user's socket/context. There is no separate daemon UID equivalent to native Linux in this setup. The broker may need to run as the desktop user, so same-UID filesystem mode bits do not isolate it from Codex. This platform is unsupported unless the effective Codex shell sandbox denies every Docker endpoint and an enforced managed MCP policy prevents project config from starting any unapproved STDIO process. Verify actual process identity, socket paths, and access after service and Codex restart.
3. Fix the Docker context and endpoint from operator-owned config. For example, Docker Desktop may use `desktop-linux` and a user socket under `~/.docker/run`; do not infer the endpoint from this example. The broker verifies the context's inspected endpoint against the configured Unix socket and rejects non-Unix endpoints.

For both platforms, configure owner-only absolute paths for the policy, local evidence, artifact, acceptance-oracle and SQLite files. Set the PostgreSQL DSN and bearer token through the service manager's protected secret channel, never command arguments, project files, MCP parameters, or logs. The token must not be available to model-generated shell commands. If Codex's effective environment filtering cannot be shown to keep it out of shell commands, do not enable the service. The present project config obtains it from `SCOUT_BROKER_TOKEN`; this behavior must be tested on the provisioned Codex profile.

## Codex administrative requirements

The sample [codex-requirements.example.toml](../../config/scout/codex-requirements.example.toml) identifies the exact server name and URL. In Codex CLI 0.170.0, an active non-empty `mcp_servers` requirements map disables direct configured servers whose name or transport identity does not match. Plugin-provided MCP servers need their own managed plugin requirements or must be disabled; do not assume the direct MCP map covers plugin servers. Do not add wildcards.

- Linux/Unix: an administrator installs `/etc/codex/requirements.toml` (administrator-owned, not writable by Codex). The CLI also recognizes legacy `/etc/codex/managed_config.toml`.
- macOS: use only a forced MDM preference `com.openai.codex:requirements_toml_base64`; an ordinary user preference is not administrative enforcement.
- Enterprise cloud requirements may add a managed layer; the policy source and resulting effective values must be checked by the administrator.

The repository example is not active policy. This host has not had an enforced policy provisioned or confirmed. Do not modify `/etc/codex`, MDM, or user-wide configuration as part of this task.

After operator provisioning, run the prepared negative probe against a disposable Codex project: it adds a synthetic STDIO command that can only create a marker in a temporary directory, plus an incorrect HTTP URL. With the managed policy active, verify the approved URL remains enabled, the extra STDIO and wrong URL are disabled, and the marker is absent. Repeat after restarting Codex and changing the project config. If the command runs or effective policy provenance cannot be shown, treat the allowlist as absent. No such host-policy probe has been run here.

## Required deployment evidence

Retain release/source hashes, worker image ID, package manifest SHA, service UID, exact executable and loaded module paths, protected config ownership/modes, Docker context/endpoint, socket denial from the Codex process, effective Codex requirements source, and reports from both service and Codex restarts. Verify unauthenticated, revoked/cross-session, invalid Host/Origin, oversized, malformed, concurrent, wrong source/revision, and disallowed tool requests. Confirm the broker process imports only the installed wheel and never accesses the checkout. The deterministic E2E now exercises HTTP MCP → broker → BC15 image → replay/acceptance, but it is not the deployment proof.

The direct BC15 writer remains a trusted in-process API and does not defend against a same-UID process moving its output directory. Only the protected broker/container composition is the supported untrusted-agent path. The worker has no network or host bind mounts; volumes remain daemon-managed and are removed only after ownership-label verification. Docker administrators, daemon compromise, container escape, and disk exhaustion beyond configured run-count/retention limits remain out of scope.

The real synthetic Codex fixture approval is expired. Do not start a real Codex fixture session until the owner explicitly grants a valid one-use approval.
