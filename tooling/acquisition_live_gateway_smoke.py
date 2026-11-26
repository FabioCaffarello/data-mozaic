"""Run a controlled Go gateway + container network smoke without external hosts."""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from acquisition_acceptance_support import configuration
from execution_coordination.adapters.execution_capability import (
    ExecutionCapabilitySigner,
)
from execution_coordination.durable_domain import AttemptLease

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deployments/compose/acquisition-live.compose.yaml"


def _write(path: Path, data: bytes, mode: int) -> None:
    path.write_bytes(data)
    path.chmod(mode)


def _issue_test_capability(
    path: Path, key_seed: bytes, policy: dict[str, object]
) -> None:
    now = datetime.now(UTC)
    lease = AttemptLease(
        tenant_id="controlled-test-tenant",
        run_id=str(uuid4()),
        attempt_id=str(uuid4()),
        generation=1,
        lease_owner="controlled-test-worker",
        lease_token="ephemeral-test-lease-token",
        lease_until=now + timedelta(seconds=25),
        attempt_deadline=now + timedelta(seconds=25),
    )
    capability = ExecutionCapabilitySigner(key_seed, key_id="gateway-test-key").issue(
        lease,
        source_id="controlled-http",
        source_revision_id="controlled-v1",
        policy_id=str(policy["policy_id"]),
        policy_version=str(policy["policy_version"]),
        policy_digest=str(policy["policy_digest"]),
        release_id="controlled-test-release",
        build_id="sha256:" + "a" * 64,
        build_manifest_digest="sha256:" + "b" * 64,
        runtime_profile_id="scrapy-live-http-v1",
        audience="data-mozaic/acquisition-gateway",
        execution_deadline=now + timedelta(seconds=25),
        now=now,
    )
    # Gateway expects the complete signed capability envelope.
    encoded_capability = (
        base64.urlsafe_b64encode(json.dumps(capability, separators=(",", ":")).encode())
        .decode("ascii")
        .rstrip("=")
    )
    path.chmod(0o600)
    _write(path, encoded_capability.encode("ascii"), 0o444)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mozaic-live-gateway-") as temp:
        temp_root = Path(temp)
        config, capability, key_seed, policy = configuration(
            temp_root / "trusted", temp_root / "tls"
        )
        result_parent = temp_root / "results"
        result_parent.mkdir(mode=0o777)
        result_parent.chmod(0o777)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            gateway_host_port = str(listener.getsockname()[1])
        env = os.environ | {
            "MOZAIC_LIVE_TRUSTED_CONFIG": str(config),
            "MOZAIC_LIVE_CAPABILITY_FILE": str(capability),
            "MOZAIC_LIVE_RESULT_PARENT": str(result_parent),
            "MOZAIC_GATEWAY_HOST_PORT": gateway_host_port,
            "MOZAIC_TEST_TLS_DIRECTORY": str(temp_root / "tls"),
            "MOZAIC_TEST_SOURCE_UID": str(os.getuid()),
        }
        project = "data-mozaic-acquisition-live-smoke"
        command = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(COMPOSE),
        ]
        worker_build = subprocess.run(
            command + ["--profile", "test", "build", "worker-scrapy-live"],
            cwd=ROOT,
            env=env,
            check=False,
        )
        if worker_build.returncode != 0:
            return worker_build.returncode
        up = subprocess.run(
            command + ["up", "--build", "--wait", "--remove-orphans", "-d"],
            cwd=ROOT,
            env=env,
            check=False,
        )
        if up.returncode != 0:
            subprocess.run(
                command + ["down", "--remove-orphans"], cwd=ROOT, env=env, check=False
            )
            return up.returncode
        try:
            _issue_test_capability(capability, key_seed, policy)
            probe = subprocess.run(
                command
                + [
                    "--profile",
                    "test",
                    "run",
                    "--rm",
                    "worker-scrapy-live",
                ],
                cwd=ROOT,
                env=env,
                check=False,
            )
            return probe.returncode
        finally:
            # Preserve the named quota/evidence volume. Containers and project
            # networks are removed only after the smoke has finished.
            subprocess.run(
                command + ["down", "--remove-orphans"], cwd=ROOT, env=env, check=False
            )


if __name__ == "__main__":
    raise SystemExit(main())
