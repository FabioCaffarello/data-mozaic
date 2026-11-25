"""Real local BC05 MongoDB + BC06 PostgreSQL/RustFS interoperability smoke."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from acquisition_output.adapters.http_custody import ArtifactCustodyHTTPAdapter
from acquisition_output.adapters.mongodb import (
    MongoOutputStore,
    MongoResources,
    MongoSettings,
)
from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import OutputSetSpec, ScopedOperationIdentity
from acquisition_output.wire import capture_output_from_wire

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT / "contracts/acquisition-output/draft/v1/fixtures/valid/artifact-output.json"
)


def request(
    path: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    tenant: str,
    token: str,
):
    req = Request(
        os.environ["ARTIFACT_CUSTODY_HTTP_URL"].rstrip("/") + path,
        data=body,
        method=method,
        headers={"Authorization": f"Bearer {token}", "X-DM-Tenant-ID": tenant},
    )
    with urlopen(req, timeout=30) as response:
        return response.read()


def restart_service() -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.artifact-custody",
            "-f",
            "deployments/compose/artifact-custody.compose.yaml",
            "restart",
            "artifact-custody",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            with urlopen("http://127.0.0.1:8086/healthz", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.5)
    raise SystemExit("BC06 service did not recover after restart")


def main() -> None:
    tenant = "tenant-local-fixture"
    source_id, revision = "synthetic-catalog", "fixture-v1"
    suffix = uuid.uuid4().hex
    operation_id, artifact_id = f"op-{suffix}", f"artifact-{suffix}"
    data = ("synthetic fixture bytes; not source evidence\n" * 400).encode()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    token = os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"]
    base = os.environ["ARTIFACT_CUSTODY_HTTP_URL"].rstrip("/")

    intent = {
        "contract_version": "artifact-custody.v1",
        "operation_id": operation_id,
        "tenant_id": tenant,
        "artifact_id": artifact_id,
        "source": {"source_id": source_id, "revision": revision},
        "artifact": {
            "artifact_id": artifact_id,
            "digest": digest,
            "size_bytes": len(data),
            "media_type": "application/octet-stream",
        },
        "representation": "downloaded_file_bytes",
        "classification": "internal",
        "retention_policy_ref": "local-fixture-v1",
    }
    post = request(
        "/draft/v1/artifact-intents",
        method="POST",
        body=json.dumps(intent).encode(),
        tenant=tenant,
        token=token,
    )
    request(
        f"/draft/v1/artifact-intents/{operation_id}/content",
        method="PUT",
        body=data,
        tenant=tenant,
        token=token,
    )
    restart_service()
    # A separate HTTP request verifies tenant/source/digest/size binding.
    query = urlencode(
        {
            "digest": digest,
            "size_bytes": len(data),
            "source_id": source_id,
            "source_revision": revision,
        }
    )
    verified = json.loads(
        request(
            f"/draft/v1/artifacts/{artifact_id}/verification?{query}",
            tenant=tenant,
            token=token,
        )
    )
    if verified.get("status") != "confirmed":
        raise SystemExit("BC06 did not return a durable confirmation")
    replay = json.loads(
        request(
            "/draft/v1/artifact-intents",
            method="POST",
            body=json.dumps(intent).encode(),
            tenant=tenant,
            token=token,
        )
    )
    replay_receipt = json.loads(
        request(
            f"/draft/v1/artifact-intents/{operation_id}/content",
            method="PUT",
            body=b"",
            tenant=tenant,
            token=token,
        )
    )
    if (
        replay.get("receipt", {}).get("receipt_id") != verified["receipt"]["receipt_id"]
        or replay_receipt["receipt_id"] != verified["receipt"]["receipt_id"]
    ):
        raise SystemExit(
            "same-operation replay did not return the original durable receipt"
        )
    changed = dict(intent)
    changed["classification"] = "confidential"
    try:
        request(
            "/draft/v1/artifact-intents",
            method="POST",
            body=json.dumps(changed).encode(),
            tenant=tenant,
            token=token,
        )
    except HTTPError as error:
        if error.code != 409:
            raise
    else:
        raise SystemExit(
            "changed intent unexpectedly reused an existing operation identity"
        )
    pending_events = json.loads(
        request("/draft/v1/outbox/pending?limit=100", tenant=tenant, token=token)
    )["events"]
    if sum(event.get("aggregate_id") == artifact_id for event in pending_events) != 1:
        raise SystemExit("expected one durable pending BC06 custody event")
    bad_query = urlencode(
        {
            "digest": "sha256:" + "0" * 64,
            "size_bytes": len(data),
            "source_id": source_id,
            "source_revision": revision,
        }
    )
    try:
        request(
            f"/draft/v1/artifacts/{artifact_id}/verification?{bad_query}",
            tenant=tenant,
            token=token,
        )
    except HTTPError as error:
        if error.code != 409:
            raise
    else:
        raise SystemExit("digest mismatch unexpectedly verified")

    custody = ArtifactCustodyHTTPAdapter(base, token)
    wire = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wire["output_id"] = f"out-{suffix}"
    wire["observation_id"] = f"obs-{suffix}"
    wire["operation"]["tenant_id"] = tenant
    wire["operation"]["operation_id"] = f"stage-{suffix}"
    wire["output_set_id"] = f"set-{suffix}"
    wire["binding"]["tenant_id"] = tenant
    wire["binding"]["source"] = {"source_id": source_id, "revision": revision}
    wire["artifacts"][0]["artifact"] = intent["artifact"]
    wire["observed_at"] = "2026-11-24T00:00:00+00:00"
    output = capture_output_from_wire(wire)
    settings = MongoSettings.from_env()

    class NoPayloadContracts:
        def validate(self, contract, payload):
            raise AssertionError("artifact-only output should not validate a payload")

    with MongoResources(settings) as resources:
        store = MongoOutputStore(resources)
        app = AcquisitionOutputApplication(
            store,
            store,
            payload_contracts=NoPayloadContracts(),
            custody=custody,
            outbox=store,
        )
        app.create_output_set(
            OutputSetSpec(
                output.output_set_id, output.binding, expected_output_count=1
            ),
            ScopedOperationIdentity(tenant, f"create-{suffix}"),
        )
        app.stage_output(output)
        sealed = app.seal_output_set(
            tenant,
            output.output_set_id,
            ScopedOperationIdentity(tenant, f"seal-{suffix}"),
        )
        app.publish_output_set(
            tenant,
            output.output_set_id,
            sealed.manifest_digest,
            ScopedOperationIdentity(tenant, f"publish-{suffix}"),
        )

    # New Python Mongo client and fresh HTTP requests verify durable cross-context reads.
    with MongoResources(settings) as resources:
        store = MongoOutputStore(resources)
        page = store.read_published(
            tenant, output.output_set_id, after_output_id=None, limit=10
        )
        if len(page) != 1 or page[0].identity.output_id != output.identity.output_id:
            raise SystemExit("published BC05 output did not survive client restart")

    downloaded = request(
        f"/draft/v1/artifacts/{artifact_id}/content", tenant=tenant, token=token
    )
    if downloaded != data or hashlib.sha256(
        downloaded
    ).hexdigest() != digest.removeprefix("sha256:"):
        raise SystemExit("BC06 returned bytes that do not match the receipt digest")

    # Wrong tenant must not be able to confirm or read the artifact.
    try:
        request(
            f"/draft/v1/artifacts/{artifact_id}/content",
            tenant="tenant-other",
            token=token,
        )
    except HTTPError as error:
        if error.code not in (403, 404):
            raise
    else:
        raise SystemExit("tenant-isolation check unexpectedly succeeded")
    print(
        json.dumps(
            {
                "evidence": "synthetic local bytes and existing draft fixture; no source exploration",
                "intent_status_bytes": len(post),
                "tenant": tenant,
                "artifact_id": artifact_id,
                "output_set_id": output.output_set_id,
                "manifest_digest": sealed.manifest_digest,
                "receipt_id": verified["receipt"]["receipt_id"],
                "outbox_event_id": next(
                    event["event_id"]
                    for event in pending_events
                    if event.get("aggregate_id") == artifact_id
                ),
                "idempotent_replay": True,
                "round_trip_bytes": len(downloaded),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
