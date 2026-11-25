"""Run a controlled Go gateway + container network smoke without external hosts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import rfc8785
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from execution_coordination.adapters.execution_capability import (
    ExecutionCapabilitySigner,
)
from execution_coordination.durable_domain import AttemptLease

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deployments/compose/acquisition-live.compose.yaml"


def _write(path: Path, data: bytes, mode: int) -> None:
    path.write_bytes(data)
    path.chmod(mode)


def _configuration(
    directory: Path, tls_directory: Path
) -> tuple[Path, Path, bytes, dict[str, object]]:
    directory.mkdir(mode=0o755, parents=True, exist_ok=False)
    directory.parent.chmod(0o755)
    tls_directory.mkdir(mode=0o755, parents=True, exist_ok=False)
    tls_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "api.fixture.test")]
    )
    not_before = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(tls_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("api.fixture.test")]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(tls_key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    _write(tls_directory / "controlled-source.crt", certificate_pem, 0o444)
    _write(
        tls_directory / "controlled-source.key",
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o400,
    )
    key_seed = os.urandom(32)
    private_key = Ed25519PrivateKey.from_private_bytes(key_seed)
    public_key = private_key.public_key().public_bytes_raw()
    policy: dict[str, object] = {
        "contract_version": "acquisition-policy/v1",
        "policy_id": "controlled-container-get",
        "policy_version": "1.0.0",
        "source_id": "controlled-http",
        "source_revision_id": "controlled-v1",
        "status": "eligible",
        "origins": ["https://api.fixture.test"],
        "methods": ["GET"],
        "path_prefixes": ["/v1/items"],
        "query_parameter_values": {},
        "dns_policy": "test-fixture-only",
        "test_destination_cidrs": ["172.30.249.10/32"],
        "redirect_policy": "deny",
        "max_requests": 4,
        "max_concurrency": 1,
        "requests_per_second": 10,
        "max_response_bytes": 2_000_000,
        "max_total_response_bytes": 4_000_000,
        "deadline_seconds": 30,
        "require_tls": False,
        "credential_refs": [],
        "evidence_retention_policy_ref": "controlled-test-retention-v1",
    }
    policy["policy_digest"] = (
        "sha256:" + hashlib.sha256(rfc8785.dumps(policy)).hexdigest()
    )
    _write(
        directory / "keys.json",
        json.dumps(
            {
                "gateway-test-key": base64.urlsafe_b64encode(public_key)
                .decode()
                .rstrip("=")
            }
        ).encode(),
        0o444,
    )
    _write(directory / "controlled-source.crt", certificate_pem, 0o444)
    _write(
        directory / "policies.json", json.dumps({"policies": [policy]}).encode(), 0o444
    )
    capability_file = directory.parent / "attempt-capability"
    _write(capability_file, b"pending", 0o444)
    return directory, capability_file, key_seed, policy


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
        config, capability, key_seed, policy = _configuration(
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
