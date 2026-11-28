# Scout trusted MCP broker deployment

**State: Linux deployment artifacts and an installable bundle path are prepared; protected deployment is not provisioned or verified.** ADR 017 remains Proposed. Do not enable controlled-source discovery until an operator provisions the isolated Linux environment, source approval and managed MCP policy, then proves service identity and process isolation.

## HTTP transport and protocol

The existing low-level MCP `Server` and tool handlers are reused through the SDK `StreamableHTTPSessionManager`; there is one business pipeline. The service entrypoint is `python -m scout.tool_host`. It binds only `127.0.0.1:8765`, serves `/mcp`, and has an authenticated `/healthz`. The HTTP adapter requires a bearer token, exact `Host: 127.0.0.1:8765`, and either no `Origin` or `http://127.0.0.1:8765`; the SDK DNS-rebinding checks are also enabled. Requests are capped at 1 MB, active HTTP requests at eight, sessions at 32, and idle sessions at 15 minutes. An absent token, invalid startup config, unavailable database, invalid policy, Docker endpoint mismatch, or missing worker image terminates startup; there is no STDIO or direct replay fallback.

The repository locks MCP Python SDK 1.30.0. Its HTTP session manager negotiates the protocol version during `initialize`; the SDK advertises `2024-11-05`, `2025-03-26`, `2025-06-18`, and `2025-11-25`. The deterministic client test negotiates through this SDK. The Codex CLI 0.170.0 handshake against an installed service still needs operator-run verification. In trusted HTTP mode the server exposes and accepts exactly the ten authoring tools listed below; BC15 replay/acceptance and administrative operations are rejected server-side even if project configuration is edited. The Streamable HTTP transport requires one endpoint supporting POST/GET and `Origin` validation; local servers should bind loopback and authenticate requests ([MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)).

## Host facts and selected topology

The inspected development host is macOS 15.6 (Darwin 24.6), arm64. Docker CLI is Docker Desktop's `desktop-linux` context, backed by a shared socket at `/Volumes/OWC Express 1M2/.docker/run/docker.sock`; the daemon reports Linux/arm64 with seccomp and cgroup namespace isolation. `multipass`, `limactl`, `vfkit`, `utmctl`, and `tart` are not installed. No protected broker process or systemd unit is installed, and port 8765 is not listening. Existing Compose services remain on the Mac and use loopback-published ports/private Docker networks.

The target is a **dedicated, separately administered Linux VM or existing Linux host**. This checkout does not provision it. It must have native Linux UID/GID and process isolation, a root-managed systemd service, an independent Docker daemon for the fixed protected worker, and no shell or Docker access for the authoring user. On this Mac, a privileged container is rejected: Docker socket access is daemon/root-equivalent within Docker Desktop, and the same developer/Codex identity can access that daemon. A native macOS broker is rejected because the trusted runtime's Linux process and UID boundaries are not reproduced. macOS is only an MCP client. No VM was available locally, so deployment remains **not ready**.

The broker listens only on Linux `127.0.0.1:8765`. On the Mac, a root-managed LaunchDaemon running as a non-login `scout-tunnel` account creates a local forward from Mac `127.0.0.1:8765` to Linux `127.0.0.1:8765` over a dedicated SSH key. The Linux SSH key entry is limited to `permitopen="127.0.0.1:8765"`, port forwarding only, and no PTY, agent forwarding, X11 or user startup files. The private key and host key database are root-owned and readable only by `scout-tunnel`; Codex cannot read the key or change the LaunchDaemon. The MCP token remains separately required, and its managed shell filter must be effective. No listener is exposed on a LAN or public interface.

The broker's proxy URL is Linux loopback `http://127.0.0.1:3128`. For the controlled fixture, run the existing `deployments/compose/scout-http-e2e.compose.yaml` topology under the Linux VM's own Docker daemon: Squid, the controlled HTTPS source, DNS fixture and blocked sink share its isolated Compose network; only Squid's host port is published to loopback. Install the synthetic CA into the protected broker's root-managed trust file; TLS verification remains enabled. Mac-local `127.0.0.1` fixture, PostgreSQL, BC06 or RustFS ports do not become reachable from the VM automatically. Use services already provisioned inside the VM or explicitly approved private/tunneled endpoints; do not bind them publicly or put their credentials in Codex. The controlled discovery flow needs the existing migrated Scout PostgreSQL schema. This topology has not been installed on Linux.

| Model | Decision | Reason |
| --- | --- | --- |
| Broker in a container on this Mac's Docker Desktop | Rejected | The agent's host identity can reach the same daemon socket; a broker container with that socket is not an independent authority boundary. |
| Native macOS broker | Rejected | No native trusted profile is implemented; Linux UID/process and isolated worker assumptions are not reproduced. |
| Separately administered Linux VM | Selected | Preserves Linux UID/GID, root-managed files, separate Docker daemon, systemd hardening and a loopback-only broker; can use existing deployment artifacts. |
| Existing separately administered Linux server | Acceptable equivalent | Only if access, cost and network are already approved; none was found or provisioned in this task. |

Linux is required by the **protected deployment profile**, not because Python or systemd are the only ways to run the source. The trusted process validates UID ownership and non-writable configuration, and delegates replay to a Linux container with network disabled. A native Linux kernel, distinct Unix identities and an independently administered Docker daemon are the trust assumptions. `systemd` is the selected supervisor and supplies the service sandbox/write-path limits; it is not itself a code-level Linux kernel requirement. The broker service's membership in the Linux `docker` group is root-equivalent on that VM, so its administrator and host remain trusted. The Codex user must not have VM shell, sudo, Docker or policy-write access.

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

The Linux bundle contains hash-locked external requirements, a Linux-targeted wheelhouse, every internal runtime wheel required by Scout/reviewer (observability, bot-build, contract-resolution, collector engineering, execution coordination, acquisition lifecycle and Scout), payload contract documents, deployment templates, the exported worker image, `uv.lock`, manifests and verifier/installer helpers. The manifest source digest covers all shipped internal package inputs and templates. The installer copies verified `contracts/`, `config/` and `deployments/` into a new staging prefix; this does not install privileged policy or activate a service. The package build refuses a non-Linux host or a target/platform mismatch. The verifier requires expected commit, Git tree, target platform, and (for an operator release) a manifest digest obtained independently. `--source-root` adds a clean-checkout/source-input comparison; `--docker-host` checks the image imported in that disposable daemon. In-bundle hashes alone are not authentication: the verifier cannot prove its own origin if both it and the package are replaced. The workflow artifact and immutable workflow revision are distribution evidence, not a cryptographic signature or independent attestation. The operator must compare source SHA/tree and manifest SHA against a trusted workflow record before installation. Never treat a prior macOS or pre-commit bundle as a Linux release.

The Linux CI job verifies both the prepared directory and a fresh download of the uploaded artifact against the clean source checkout and the pre-upload manifest digest. This ensures the bytes delivered by Actions are the same complete bundle the job validated. The packager removes the specific non-package `wheels/.gitignore` generated by `uv build`; unexpected hidden wheel output is rejected rather than relying on artifact-upload settings that omit hidden files.

`install_scout_bundle.sh BUNDLE PREFIX EXPECTED_COMMIT EXPECTED_TREE TARGET EXPECTED_MANIFEST_SHA256 DOCKER_HOST` verifies the bundle and imported image before installing only into a new unprivileged staging directory. It does not use sudo, load an image, create a service, or write administrative policy. First verify the bundle offline, then load the reviewed worker archive into the intended protected daemon, then run the installer with that daemon endpoint so it verifies the imported image. The operator copies the unprivileged staging tree to a new root-owned release path and separately installs service/policy configuration. The broker never builds an image; it accepts only the operator-configured image ID (`sha256:…`) and fixed context/endpoint, then verifies that image before exposing protected operations.

## Operator provisioning

### Linux with native Docker

1. Install the verified bundle under a root-owned versioned path such as `/opt/data-mozaic/scout-broker/<version>`. Make release files non-writable by both the broker and Codex identities. Give a dedicated `scout-broker` service UID read/execute access to the venv and write access only to its `/var/lib/data-mozaic/scout` state, evidence, and artifact roots.
2. Load the reviewed worker archive through the intended Docker daemon. Recompute the SHA-256 of the config blob named in archive `manifest.json`; it must equal `worker-manifest.json:worker_image_id`, which must also equal `archive_config_digest` and the imported Docker `.Id`. Verify `.Os=linux`, `.Architecture` matches the target, and compare entrypoint, command, user, working directory, and rootfs diff IDs. Configure `SCOUT_BC15_WORKER_IMAGE_DIGEST`, `SCOUT_DOCKER_EXECUTABLE`, `SCOUT_DOCKER_CONTEXT`, and `SCOUT_DOCKER_ENDPOINT` in a root-owned service environment file. Do not use a mutable tag as image identity.
3. Grant the broker service identity access to the Docker socket. Membership in the `docker` group is effectively root-equivalent; Codex must use another UID, must not be in that group, and must fail an OS-level socket connect probe. The daemon and all service files/configuration need independent root ownership.
4. Resolve the Docker CLI path as the root-owned executable's real path. Check the file and every parent directory are root-owned and not group/world writable; record symlink resolution, mode, inode/hash and version. The broker validates these conditions at startup and invokes that absolute path with a fixed PATH and no shell. Ensure the service PATH cannot shadow it. Keep Docker context config/service environment root-owned, pin the Unix endpoint, and omit `DOCKER_HOST`, `DOCKER_CONTEXT`, `PYTHONPATH`, and workspace-derived paths from the service environment.
5. Use a root-owned systemd unit with `User=scout-broker`, `WorkingDirectory=/`, `ExecStart=/opt/.../venv/bin/python -I -m scout.tool_host`, a protected environment file, restart-on-failure, and write paths limited to declared state/data roots. Keep release code and venv read-only to the service. Set `PYTHONDONTWRITEBYTECODE=1`. Verify the running process's executable, argv, environment allowlist, working directory, module origins and ownership after every restart.

### Linux first-install procedure (operator, not run from the Mac checkout)

The bundle is not yet published or built for this uncommitted M8 tree. After an independently reviewed Linux CI workflow produces it, set `BUNDLE`, `COMMIT`, `TREE`, `MANIFEST_SHA`, and `TARGET` from the release record on the Linux VM. `BUNDLE` is the downloaded artifact directory; `COMMIT` and `TREE` are full Git object IDs, and `MANIFEST_SHA` is copied from independent release evidence, not trusted from the same download.

```sh
python3 -B "$BUNDLE/verify_scout_bundle.py" "$BUNDLE" \
  --expected-commit "$COMMIT" --expected-tree "$TREE" \
  --target-platform "$TARGET" --expected-manifest-sha256 "$MANIFEST_SHA"
docker load --input "$BUNDLE/bc15-worker-image.tar"
WORKER_IMAGE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["worker_image_id"])' "$BUNDLE/worker-manifest.json")"
test "$(docker image inspect --format '{{.Id}}' "$WORKER_IMAGE_ID")" = "$WORKER_IMAGE_ID"
STAGING="/var/tmp/data-mozaic-scout-staging-$COMMIT"
sh "$BUNDLE/install_scout_bundle.sh" "$BUNDLE" "$STAGING" \
  "$COMMIT" "$TREE" "$TARGET" "$MANIFEST_SHA" unix:///var/run/docker.sock
```

Offline verification must pass before `docker load`; the service uses the same daemon. The installer destination must not exist beforehand. Then an administrator installs into a new immutable release path:

```sh
RELEASE_ROOT=/opt/data-mozaic/scout-broker
RELEASE="$RELEASE_ROOT/releases/$COMMIT"
test ! -e "$RELEASE"
sudo install -d -o root -g root -m 0755 "$RELEASE"
sudo cp -R "$STAGING/." "$RELEASE/"
sudo chown -R root:root "$RELEASE"
sudo chmod -R go-w "$RELEASE"
sudo install -o root -g root -m 0644 \
  "$RELEASE/deployments/systemd/data-mozaic-scout-broker.service" \
  /etc/systemd/system/data-mozaic-scout-broker.service
test ! -e "$RELEASE_ROOT/current"
sudo ln -s "releases/$COMMIT" "$RELEASE_ROOT/current"
sudo systemctl daemon-reload
```

The unit name is `data-mozaic-scout-broker.service`. Do not replace `current` in place during this first install. For later releases, stop the service, retain the prior immutable directory and use an independently reviewed symlink-switch procedure. Do not run these commands on the Mac's Docker Desktop daemon.

The administrator provisions (only if absent) non-login Linux identities: `scout-broker` (primary group `scout-broker`, member of `docker` and `scout-evidence-reviewers`), `scout-reviewer` (primary group `scout-reviewer`, member of `scout-evidence-reviewers` only), and `scout-mcp-tunnel` (SSH forwarding only). Do not overwrite existing IDs. Resolve numeric IDs with `id -u scout-broker`, `id -u scout-reviewer`, and `getent group scout-evidence-reviewers`. The identity map sets `agent_uid` to the broker UID and maps the actual reviewer UID to its principal and exact SourceRevision scope. The remote Codex author has no Linux login.

For a new VM only, first inspect `getent group docker`, `getent passwd scout-broker scout-reviewer scout-mcp-tunnel` and `getent group scout-broker scout-reviewer scout-evidence-reviewers scout-mcp-tunnel`. If any named account/group already exists, do not run the corresponding creation command; inspect ownership and resolve IDs with the operator. If absent, the commands are:

```sh
sudo groupadd --system scout-broker
sudo groupadd --system scout-reviewer
sudo groupadd --system scout-evidence-reviewers
sudo groupadd --system scout-mcp-tunnel
sudo useradd --system --no-create-home --home-dir /var/lib/data-mozaic/scout \
  --shell /usr/sbin/nologin --gid scout-broker \
  --groups docker,scout-evidence-reviewers scout-broker
sudo useradd --system --no-create-home --home-dir /var/lib/data-mozaic/scout-reviewer \
  --shell /usr/sbin/nologin --gid scout-reviewer \
  --groups scout-evidence-reviewers scout-reviewer
sudo useradd --system --no-create-home --home-dir /var/empty \
  --shell /usr/sbin/nologin --gid scout-mcp-tunnel scout-mcp-tunnel
```

Create the three new data directories only after confirming they do not already contain data:

```sh
sudo install -d -o scout-broker -g scout-broker -m 0700 /var/lib/data-mozaic/scout
sudo install -d -o scout-broker -g scout-evidence-reviewers -m 2750 /var/lib/data-mozaic/scout-evidence
sudo install -d -o scout-broker -g scout-broker -m 0700 /var/lib/data-mozaic/scout-artifacts
```

Create `/var/lib/data-mozaic/scout` as `scout-broker:scout-broker` mode `0700`; evidence root `/var/lib/data-mozaic/scout-evidence` as `scout-broker:scout-evidence-reviewers` mode `2750`; artifact root `/var/lib/data-mozaic/scout-artifacts` as `scout-broker:scout-broker` mode `0700`. Set `SCOUT_EVIDENCE_REVIEWER_GID` to the actual evidence group GID. The broker creates evidence objects and receipts as `0640` inside setgid `2750` subdirectories so the non-login reviewer operation can verify them. A private root or wrong GID fails closed. The evidence group can read all local Scout custody objects, so only the restricted reviewer account belongs to it; it has no interactive shell and is invoked only by the privileged reviewer command below. Codex is not a member.

Install the reviewed `broker.env.example` as root-owned `/etc/data-mozaic/scout/broker.env`, mode `0600`, replacing placeholders through the VM's protected secret channel. Set the worker image ID from the verified manifest and fixed Docker executable/context/endpoint. Install the merged `authoring-policy.json` as `root:scout-evidence-reviewers`, mode `0640`: the service is a member of that read group, while only root can change the policy. Install reviewer env as `root:scout-reviewer` mode `0640`, reviewer identity policy root-owned mode `0640`, root-owned Docker context config and the payload contract registry from the bundle. Configure the approved synthetic CA path and egress proxy from the controlled fixture running under this Linux daemon. Keep TLS verification enabled.

For example, after the source owner has reviewed and merged the exact grant into the protected policy, install it without overwriting an existing file:

```sh
test -f /etc/data-mozaic/scout/authoring-policy.json
sudo install -o root -g scout-evidence-reviewers -m 0640 \
  /secure/approved/authoring-policy.json \
  /etc/data-mozaic/scout/authoring-policy.json
sudo -u scout-broker test -r /etc/data-mozaic/scout/authoring-policy.json
sudo -u scout-reviewer test -r /etc/data-mozaic/scout/authoring-policy.json
sudo stat -c '%U:%G %a %n' /etc/data-mozaic/scout/authoring-policy.json
sudo sha256sum /etc/data-mozaic/scout/authoring-policy.json
```

`/secure/approved/authoring-policy.json` is an operator-created merged policy input, not the request JSON or the empty example template. Before installation, the source owner must verify that existing sources/approvals are preserved and that the exact source identity, principal, grant, limits and expiry match the approved request. The group grants read access only; it does not grant policy-write access.

Install the supplied SSH Match block and restricted public key under root-owned `/etc/ssh/authorized_keys/scout-mcp-tunnel`; validate the full configuration with `sshd -t` before reloading SSH. The key permits only `127.0.0.1:8765`; it has no shell, TTY, agent forwarding, X11 forwarding or user RC. On the Mac, install the tunnel plist/config/key under administrator-managed paths, owned by the non-login `scout-tunnel` account and unreadable to Codex. Do not reuse a developer key.

Start and inspect the broker only after protected config is installed:

```sh
sudo systemctl enable --now data-mozaic-scout-broker.service
sudo systemctl is-active data-mozaic-scout-broker.service
sudo journalctl -u data-mozaic-scout-broker.service --since=-5min --no-pager
```

Check the authenticated health endpoint through the protected operator channel without putting the token in shell history or argv. Verify the process UID/GID, executable, argv, cwd `/`, environment variable names only, isolated Python module origins under `current/venv`, root-owned release files and Codex's inability to connect to the Linux Docker socket. Stop with `sudo systemctl stop data-mozaic-scout-broker.service`; this preserves state and volumes.

For a local health probe on the Linux VM, a privileged operator can read only the token field from the root-owned environment file into a non-exported shell variable. Bash process substitution gives `curl` a config file descriptor, so the token is absent from its command line and shell history:

```sh
sudo bash -c '
  set +x
  token=$(awk -F= '\''$1 == "SCOUT_BROKER_TOKEN" { sub(/^[^=]*=/, ""); print; exit }'\'' /etc/data-mozaic/scout/broker.env)
  test -n "$token"
  curl --fail --silent --show-error \
    --config <(printf '\''header = "Authorization: Bearer %s"\n'\'' "$token") \
    http://127.0.0.1:8765/healthz
  result=$?
  unset token
  exit "$result"
'
```

This is an authenticated liveness check, not proof of MCP session authorization or source approval. The operator separately checks systemd `MainPID`, `/proc/<pid>/status` UID/GID, `/proc/<pid>/cwd`, executable, command arguments, environment **names only**, and imported module paths without printing secret values.

### Reviewer promotion operation

The reviewer map is a template until an operator provisions the real UID. The proposal handoff supplies `session_id`, `proposal_id`, `proposal_revision`, and canonical `candidate_sha256`. The human reviewer compares those exact values with proposal history and validation output. Promotion runs as a separate non-root, non-login UID and does not approve a release or install authority:

```sh
sudo systemd-run --wait --pipe --collect \
  --uid=scout-reviewer --gid=scout-reviewer \
  --property=SupplementaryGroups=scout-evidence-reviewers \
  --property=EnvironmentFile=/etc/data-mozaic/scout/reviewer.env \
  --property=WorkingDirectory=/ \
  /opt/data-mozaic/scout-broker/current/venv/bin/scout-review promote \
  --session-id '<SESSION_ID_FROM_HANDOFF>' \
  --proposal-id '<PROPOSAL_ID_FROM_HISTORY>' \
  --proposal-revision '<REVISION_FROM_HISTORY>' \
  --candidate-sha256 '<SHA256_FROM_VALIDATION>' \
  --confirm-candidate-sha256 '<SAME_SHA256_FROM_HANDOFF>'
```

The program derives reviewer identity from the process UID, rechecks the exact proposal revision/digest and registers only the immutable Playbook. If registration outcome is ambiguous, retry with the same values. Do not edit a proposal or treat promotion as release approval.

### macOS as the managed MCP client

Do not install the broker on macOS or grant it Docker Desktop access. The supplied `deployments/macos/org.datamozaic.scout-tunnel.plist.example` and `config/scout/scout-tunnel-ssh-config.example` are templates for the client-side tunnel only. A Mac administrator installs them under `/Library/LaunchDaemons` and `/etc/data-mozaic/scout`, creates the non-login `scout-tunnel` account, and installs a dedicated SSH private key readable only by that account. The Linux account's `authorized_keys` entry must restrict the key to `permitopen="127.0.0.1:8765"`, `no-pty`, no agent/X11 forwarding and no user startup files; the server-side `sshd` configuration must allow only the corresponding local-forward operation. Pin the VM host key in the root-owned `known_hosts` file. Never reuse a developer SSH key.

The administrator first creates the non-login `scout-tunnel` account and obtains a dedicated SSH key through the approved key-management channel. The public key is installed on Linux only after the host fingerprint and SSH principal are verified. After verifying the template substitutions and host key out of band, install the client files with these paths and modes:

```sh
sudo install -d -o root -g wheel -m 0755 /etc/data-mozaic/scout
sudo install -o root -g wheel -m 0644 \
  config/scout/scout-tunnel-ssh-config.example \
  /etc/data-mozaic/scout/tunnel_ssh_config
sudo install -o scout-tunnel -g wheel -m 0600 \
  /secure/operator-channel/tunnel_ed25519 \
  /etc/data-mozaic/scout/tunnel_ed25519
sudo install -o root -g wheel -m 0644 \
  /secure/operator-channel/verified_known_hosts \
  /etc/data-mozaic/scout/known_hosts
sudo install -o root -g wheel -m 0644 \
  deployments/macos/org.datamozaic.scout-tunnel.plist.example \
  /Library/LaunchDaemons/org.datamozaic.scout-tunnel.plist
sudo plutil -lint /Library/LaunchDaemons/org.datamozaic.scout-tunnel.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/org.datamozaic.scout-tunnel.plist
sudo launchctl print system/org.datamozaic.scout-tunnel
```

`/secure/operator-channel/tunnel_ed25519` and `verified_known_hosts` are administrator-provided inputs, not repository artifacts. The SSH config's VM address must be the assigned private VM address; do not accept an unverified `ssh-keyscan` result. Check `sudo launchctl bootout system /Library/LaunchDaemons/org.datamozaic.scout-tunnel.plist` for shutdown. Verify that only `127.0.0.1:8765` listens locally and that the SSH process runs as `scout-tunnel`. The Codex user can reach that loopback port but cannot read the SSH key or change the launch daemon. MCP bearer authentication and the forced managed Codex policy are still required; a port forward is not an approval.

## Rollback

Keep the prior root-owned release directory and worker image until the new version passes its operator checks. To roll back, stop the service (`systemctl stop ...` on Linux or the operator-managed `launchctl` job on macOS), restore the prior versioned executable/config reference and matching worker image ID, then restart and check authenticated `/healthz` and the service log. Keep the SQLite registry and labeled volumes intact; do not run global Docker cleanup or remove volumes during rollback. This change currently keeps registry schema version `1`; if a future release changes it incompatibly, stop and take an operator-controlled SQLite backup before upgrade, and use that backup only with the matching prior binary. Confirm MCP tool availability and policy again after Codex restart.

On Linux, configure absolute paths for policy, local evidence, artifact, optional acceptance oracle and SQLite state. Set the PostgreSQL DSN and bearer token through systemd's root-owned protected environment file, never command arguments, project files, MCP parameters, or logs. The MCP bearer token authenticates only the fixed broker API; it grants no Docker API access by itself. On the Mac, Codex resolves the token through `SCOUT_BROKER_TOKEN`; the forced MDM shell filter below must exclude it from agent shell children.

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

The prepared request is [scout-controlled-discovery-approval.json](requests/scout-controlled-discovery-approval.json), SHA-256 `cc3dfdc43453df966e52b05383e88e18f3d526e9cce47140edfea8cf76185ef2`. It requests only `e2e-catalog-single@single-resource-v1`, one HTTPS `GET` to `https://catalog.fixture.test/api/catalog`, one request/page, 4096 response bytes, 120 seconds, one use, and an expiry set by the approver no later than 30 minutes after grant. Its path grant is `/api/catalog`; the runtime itself supports GET only. The protected host's configured principal must remain exactly `scout-local-acceptance`; verify `SCOUT_LOCAL_PRINCIPAL` in the root-owned environment file and never silently substitute it. The request JSON is a review artifact, not a policy file accepted by the runtime and not an approval.

At grant time, the source owner reviews and installs the source record and approval under `/etc/data-mozaic/scout/authoring-policy.json`, preserving all unrelated entries. The policy schema is `config/scout/authoring-policy.example.json`: `sources[]` has exact identity, grants and limits; `approvals[]` has principal, same identity/grants, limits and timezone-aware `expires_at`. The exact source and approval entries are:

```json
{
  "source_id": "e2e-catalog-single",
  "revision": "single-resource-v1",
  "grants": [{"origin": "https://catalog.fixture.test", "path_prefixes": ["/api/catalog"]}],
  "limits": {"max_requests": 1, "max_duration_seconds": 120, "max_response_bytes": 4096}
}
```

```json
{
  "principal": "scout-local-acceptance",
  "source_id": "e2e-catalog-single",
  "revision": "single-resource-v1",
  "grants": [{"origin": "https://catalog.fixture.test", "path_prefixes": ["/api/catalog"]}],
  "expires_at": "<UTC_TIMESTAMP_NO_MORE_THAN_1800_SECONDS_AFTER_APPROVAL>",
  "limits": {"max_requests": 1, "max_duration_seconds": 120, "max_response_bytes": 4096}
}
```

The policy model has no independent `max_pages` field; the preflight reports one page because the effective `max_requests` is one. The protected server's SQLite operation ledger claims this exact SourceRevision approval once at `scout_start`; keep `/var/lib/data-mozaic/scout` persistent and private. The approver must verify there is no active prior grant or consumed single-use claim. There is no grant signing API in this profile: authority is the explicitly reviewed, root-owned, non-writable managed policy installation. The approver signs/records its change through the organization's existing administrative process, not by inventing a runtime signature. Do not use `make scout-dev-authorize-single-fixture`; it edits local development policy only.

After the source owner approves the exact request, the Linux administrator merges the entries into the protected policy (do not replace it with the request JSON), checks JSON validity and ownership/mode, and records `sha256sum /etc/data-mozaic/scout/authoring-policy.json` in the approval record. There is no separate signing operation in the current local policy adapter. Then restart the broker and run preflight through a fresh authorized MCP session. Expected `scout_preflight` success is `ok=true` for exactly that source/revision, origin `https://catalog.fixture.test`, path prefix `/api/catalog`, GET-only runtime behavior, and limits `max_requests=1`, derived `max_pages=1`, `max_response_bytes=4096`; only then may a fresh authorized session start and consume the approval once. If the grant expires unused, the source owner repeats the same review/install process with a new short-lived expiry; never reactivate or widen the expired approval.

## Current host readiness and startup handoff

The current development host is macOS. On 2026-11-27, `make scout-dev-status` reported local migrations, fixtures, launcher configuration, Codex CLI/authentication, and MCP registration ready, but `source_approval` blocked. `http://127.0.0.1:8765/healthz` refused the connection, no `scout.tool_host` process or Scout launchd service was found, and no Linux VM/tooling was provisioned. The project MCP endpoint is registered, but that is not proof of active-session tool availability. The active tool inventory for this coding session does not contain Scout tools.

The protected deployment procedure is supported on Linux only. There is no safe broker command to run on this Mac. Readiness order is: (1) publish a reviewed commit and obtain the Linux CI bundle/release evidence; (2) provision the separately administered Linux VM/host and isolated database/fixture network; (3) install identities, root-owned files and worker image; (4) start/check the systemd broker and validate TLS/source reachability; (5) install the root-managed Mac SSH tunnel and forced MDM MCP server identity; (6) start a fresh Codex session and prove effective authoring tools and token filtering; (7) only then obtain/install the short-lived one-use source grant; (8) run exact preflight, one discovery and proposal validation; (9) hand off to the separate reviewer command. Do not start the approval window before steps 1–6 pass. Do not substitute `python -m scout.tool_host` from this checkout, a workspace launcher, direct HTTP calls, or the Mac Docker daemon.

## Minimal managed MCP request and tool set

The current project config was narrowed to the authoring set below, and trusted HTTP mode now exposes exactly this set from the server itself. It rejects direct calls to other tool names; changing the project `enabled_tools` cannot enable tools the server does not expose. The managed requirements example pins only the exact `data_mozaic_scout` server identity/URL and excludes the broker token from shell children. It does not claim a managed per-tool list that Codex's installed requirements schema has not established. The active managed session was not available to inspect.

```text
scout_preflight
scout_start
scout_fetch
scout_record_finding
scout_propose
scout_status
scout_export_transcript
scout_proposal_history
scout_validate_proposal
scout_prepare_promotion
```

`make acquisition-capabilities` is the deterministic repository-local capability catalog; it does not need an MCP permission. The administrator installs the existing requirements example through forced MDM on macOS (`com.openai.codex:requirements_toml_base64`), preserves `SCOUT_BROKER_TOKEN = "exclude"`, pins only this server identity, and verifies that no other managed MCP or plugin route grants reviewer promotion, release approval, signing, installation or policy administration. Restart Codex and capture managed-policy provenance/hash plus the actual effective tool catalog. `codex mcp list --json` and project `.codex/config.toml` show configured registration only, not effective managed permissions. This coding-agent session does not expose Scout tools.

## Disk and capacity operations

Docker named volumes do not have a physical disk quota in this implementation. Run count, concurrency, worker memory/CPU/PID limits, body limits, and 24-hour retention constrain application behavior but do not cap Docker Desktop VM or Linux filesystem consumption. Monitor the Docker data root/VM free space and volume growth. The operator must define and alert on a minimum free-space threshold before enabling the broker; stop accepting new replay/acceptance work below it, preserve in-retention evidence, and investigate/clean only volumes the broker's ownership labels and registry prove it owns. Never use global `docker system prune` or `volume prune` for this service.
