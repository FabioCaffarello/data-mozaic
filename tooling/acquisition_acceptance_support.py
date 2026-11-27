"""Test-only composition helpers shared by controlled acquisition harnesses."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import rfc8785
from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import ScopedOperationIdentity
from acquisition_output.wire import publication_receipt_to_wire
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID


class InProcessBC05Publisher:
    """Compose a local BC05 app as BC04 publisher, with bounded ACK-loss injection."""

    def __init__(self, app: AcquisitionOutputApplication, *, lose_ack: bool = False):
        self._app = app
        self._lose_ack = lose_ack

    def publish_grant(self, grant):
        receipt = self._app.publish_output_set(
            grant.tenant_id,
            grant.output_set_id,
            grant.manifest_digest,
            ScopedOperationIdentity(grant.tenant_id, grant.operation_id),
            publication_grant=grant.fields(),
        )
        if self._lose_ack:
            self._lose_ack = False
            raise TimeoutError("synthetic lost acknowledgment after BC05 commit")
        return publication_receipt_to_wire(receipt)

    def get_publication_receipt(self, tenant_id: str, output_set_id: str):
        receipt = self._app.get_publication_receipt(tenant_id, output_set_id)
        return publication_receipt_to_wire(receipt) if receipt is not None else None


def key_material() -> tuple[bytes, str, bytes]:
    """Read only the preserved local BC04 test signing seed; never logs it."""
    try:
        seed = bytes.fromhex(os.environ["BC04_GRANT_SIGNING_KEY"])
        key_id = os.environ["BC04_GRANT_KEY_ID"]
    except (KeyError, ValueError) as error:
        raise RuntimeError(
            "local fixture grant key configuration is invalid"
        ) from error
    if len(seed) != 32 or not key_id:
        raise RuntimeError("local fixture grant key configuration is invalid")
    public = (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return seed, key_id, public


def artifact_request(tenant_id: str, artifact_id: str) -> bytes:
    """Read bounded bytes through the local BC06 public content endpoint."""
    from urllib.parse import quote

    url = (
        os.environ["ARTIFACT_CUSTODY_HTTP_URL"].rstrip("/")
        + f"/draft/v1/artifacts/{quote(artifact_id, safe='')}/content"
    )
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['ARTIFACT_CUSTODY_LOCAL_TOKEN']}",
            "X-DM-Tenant-ID": tenant_id,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read(2_000_001)
            if len(body) > 2_000_000 or response.read(1):
                raise RuntimeError("BC06 artifact exceeds the local read bound")
            return body
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RuntimeError("BC06 could not read the controlled artifact") from error


def artifact_request_identity(tenant_id: str, artifact_id: str) -> tuple[int, str]:
    """Independently retrieve public BC06 content with a fixed hashing buffer."""
    from urllib.parse import quote

    url = (
        os.environ["ARTIFACT_CUSTODY_HTTP_URL"].rstrip("/")
        + f"/draft/v1/artifacts/{quote(artifact_id, safe='')}/content"
    )
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['ARTIFACT_CUSTODY_LOCAL_TOKEN']}",
            "X-DM-Tenant-ID": tenant_id,
        },
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with urlopen(request, timeout=30) as response:
            while chunk := response.read(64 << 10):
                size += len(chunk)
                if size > 2_000_000:
                    raise RuntimeError("BC06 artifact exceeds the local read bound")
                digest.update(chunk)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RuntimeError("BC06 could not read the controlled artifact") from error
    return size, "sha256:" + digest.hexdigest()


def write(path: Path, data: bytes, mode: int) -> None:
    path.write_bytes(data)
    path.chmod(mode)


def configuration(
    directory: Path, tls_directory: Path
) -> tuple[Path, Path, bytes, dict[str, object]]:
    """Create temporary keys and policy for the local controlled gateway only."""
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
    write(tls_directory / "controlled-source.crt", certificate_pem, 0o444)
    write(
        tls_directory / "controlled-source.key",
        tls_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o400,
    )
    key_seed = os.urandom(32)
    public_key = (
        Ed25519PrivateKey.from_private_bytes(key_seed).public_key().public_bytes_raw()
    )
    policy = {
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
    write(
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
    write(directory / "controlled-source.crt", certificate_pem, 0o444)
    write(
        directory / "policies.json", json.dumps({"policies": [policy]}).encode(), 0o444
    )
    capability_file = directory.parent / "attempt-capability"
    write(capability_file, b"pending", 0o444)
    return (
        directory,
        capability_file,
        key_seed,
        policy,
    )
