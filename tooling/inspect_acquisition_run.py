"""Inspect one authorized acquisition through its owner public interfaces."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from acquisition_application.adapters.artifact_custody import ArtifactCustodyHTTPClient
from acquisition_output.adapters.mongodb import MongoOutputStore, MongoResources
from acquisition_output.adapters.mongodb.config import MongoSettings
from acquisition_output.application import AcquisitionOutputApplication
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from work_intake.adapters.postgres import PostgresWorkIntakeStore


class _ReadOnlyComposition:
    def validate(self, *_args, **_kwargs) -> None:
        raise RuntimeError("inspection composition is read-only")

    def confirm(self, *_args, **_kwargs):
        raise RuntimeError("inspection composition is read-only")


def _receipt(value):
    if value is None:
        return None
    return {
        "tenant_id": value.tenant_id,
        "output_set_id": value.output_set_id,
        "manifest_digest": value.manifest_digest,
        "publication_id": value.publication_id,
        "finalization_reservation_id": value.finalization_reservation_id,
        "published_at": value.published_at.isoformat(),
    }


def _receipt_matches_run(receipt, run_receipt, tenant_id, reservation) -> bool:
    if receipt is None or not isinstance(run_receipt, dict):
        return False
    return (
        receipt.tenant_id == tenant_id == run_receipt.get("tenant_id")
        and receipt.output_set_id
        == reservation.output_set_id
        == run_receipt.get("output_set_id")
        and receipt.manifest_digest
        == reservation.manifest_digest
        == run_receipt.get("manifest_digest")
        and receipt.publication_id == run_receipt.get("publication_id")
        and receipt.finalization_reservation_id
        == reservation.reservation_id
        == run_receipt.get("finalization_reservation_id")
    )


def _bc06_metadata(tenant_id: str, artifact_id: str) -> dict[str, object]:
    endpoint = os.environ.get("ARTIFACT_CUSTODY_HTTP_URL", "").rstrip("/")
    token = os.environ.get("ARTIFACT_CUSTODY_LOCAL_TOKEN", "")
    if not endpoint or not token:
        return {
            "state": "unavailable",
            "reason": "BC06 endpoint or local token is not configured",
        }
    request = Request(
        f"{endpoint}/draft/v1/artifacts/{quote(artifact_id, safe='')}",
        headers={
            "Authorization": "Bearer " + token,
            "X-DM-Tenant-ID": tenant_id,
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            value = json.loads(response.read(64 << 10))
    except HTTPError as error:
        return {"state": "unavailable", "reason": f"BC06 returned HTTP {error.code}"}
    except (URLError, TimeoutError, OSError, ValueError) as error:
        return {
            "state": "unavailable",
            "reason": f"BC06 metadata read failed ({type(error).__name__})",
        }
    artifact = value.get("artifact") if isinstance(value, dict) else None
    if not isinstance(artifact, dict) or artifact.get("artifact_id") != artifact_id:
        return {
            "state": "unresolved",
            "reason": "BC06 metadata response did not bind the requested ArtifactID",
        }
    return {
        "state": value.get("state", "unknown"),
        "artifact_id": artifact_id,
        "digest": artifact.get("digest"),
        "size_bytes": artifact.get("size_bytes"),
        "source": value.get("source"),
        "representation": value.get("representation"),
    }


def _bc06_inventory(
    tenant_id: str, *, cursor: str | None, limit: int
) -> dict[str, object]:
    endpoint = os.environ.get("ARTIFACT_CUSTODY_HTTP_URL", "")
    token = os.environ.get("ARTIFACT_CUSTODY_LOCAL_TOKEN", "")
    if not endpoint or not token:
        return {
            "state": "unavailable",
            "reason": "BC06 endpoint or local token is not configured",
        }
    try:
        return ArtifactCustodyHTTPClient(endpoint, token).list_inventory(
            tenant_id=tenant_id, cursor=cursor, limit=limit
        )
    except Exception as error:  # noqa: BLE001 - preserve owner failure as explicit inspection data.
        return {
            "state": "unresolved",
            "reason": f"BC06 inventory failed ({type(error).__name__})",
        }


def inspect(
    *,
    tenant_id: str,
    submission_id: str | None,
    run_id: str | None,
    custody_cursor: str | None = None,
    custody_limit: int = 50,
) -> dict[str, object]:
    bc04_dsn = os.environ.get("BC04_DATABASE_URL", "")
    if not bc04_dsn:
        raise RuntimeError("BC04_DATABASE_URL is required")
    run_repository = PostgresDurableRunRepository(bc04_dsn)
    submission = None
    if submission_id is not None:
        work_intake_dsn = os.environ.get("WORK_INTAKE_DATABASE_URL", "")
        if not work_intake_dsn:
            raise RuntimeError(
                "WORK_INTAKE_DATABASE_URL is required with --submission-id"
            )
        store = PostgresWorkIntakeStore(work_intake_dsn, Path("migrations"))
        submission = store.get_submission(tenant_id, submission_id)
        if submission is None:
            raise RuntimeError("BC13 Submission does not exist for this tenant")
        if run_id is not None and submission.run_id != run_id:
            raise RuntimeError("Submission resolves to a different CaptureRun")
        run_id = submission.run_id
    if run_id is None:
        raise RuntimeError("Submission has no CaptureRun")

    result: dict[str, object] = {
        "tenant_id": tenant_id,
        "requested_submission_id": submission_id,
        "requested_run_id": run_id,
        "consistency": "owner reads occur independently; this is not a global snapshot",
        "work_intake": (
            {
                "state": submission.state.value,
                "submission_id": submission.intent.submission_id,
                "input_id": submission.intent.input_id,
                "source_id": submission.intent.source_id,
                "source_revision_id": submission.intent.source_revision_id,
                "release_id": submission.intent.release_id,
                "run_id": submission.run_id,
                "claim_generation": submission.claim_generation,
                "diagnostic_code": submission.diagnostic_code,
            }
            if submission is not None
            else {
                "state": "not_requested",
                "reason": "inspection started from CaptureRun",
            }
        ),
    }
    run = run_repository.get(tenant_id, run_id)
    if run is None:
        result["execution"] = {
            "state": "unavailable",
            "reason": "BC04 CaptureRun not found for tenant",
        }
        return result
    reservation = run.reservation
    result["execution"] = {
        "state": run.state.value,
        "tenant_id": run.intent.tenant_id,
        "run_id": run.intent.run_id,
        "request_id": run.intent.request_id,
        "attempt_id": run.attempt_id,
        "attempt_state": run.attempt_state.value if run.attempt_state else None,
        "generation": run.generation,
        "lease_until": run.lease_until.isoformat() if run.lease_until else None,
        "deadline": run.intent.deadline.isoformat(),
        "release_id": run.intent.binding.release_id,
        "reservation": (
            {
                "reservation_id": reservation.reservation_id,
                "attempt_id": reservation.attempt_id,
                "generation": reservation.fencing_generation,
                "output_set_id": reservation.output_set_id,
                "manifest_digest": reservation.manifest_digest,
            }
            if reservation is not None
            else None
        ),
        "publication_receipt": run.publication_receipt,
    }
    if reservation is None:
        result["acquisition_output"] = {
            "state": "unavailable",
            "reason": "BC04 has no finalization reservation",
        }
        result["fetch_evidence"] = {
            "state": "unresolved",
            "reason": "Gateway evidence identities are not indexed by BC04",
        }
        result["residual_custody"] = _bc06_inventory(
            tenant_id, cursor=custody_cursor, limit=custody_limit
        )
        return result

    with MongoResources(MongoSettings.from_env()) as resources:
        store = MongoOutputStore(resources)
        app = AcquisitionOutputApplication(
            store,
            store,
            payload_contracts=_ReadOnlyComposition(),
            custody=_ReadOnlyComposition(),
        )
        snapshot = app.inspect_output_set(tenant_id, reservation.output_set_id)
        receipt = app.get_publication_receipt(tenant_id, reservation.output_set_id)
        if snapshot is None:
            result["acquisition_output"] = {
                "state": "unavailable",
                "reason": "BC05 OutputSet not found",
            }
            outputs = ()
            next_cursor = None
        else:
            page = app.read_published_outputs(
                tenant_id, reservation.output_set_id, limit=100
            )
            outputs = page.outputs
            next_cursor = page.next_cursor
            result["acquisition_output"] = {
                "state": snapshot.state.value,
                "output_set_id": snapshot.output_set_id,
                "manifest_digest": snapshot.manifest_digest,
                "member_count": snapshot.member_count,
                "artifact_reference_count": snapshot.artifact_reference_count,
                "custody_reference_count": snapshot.custody_reference_count,
                "publication_receipt": _receipt(receipt),
                "receipt_matches_bc04": _receipt_matches_run(
                    receipt, run.publication_receipt, tenant_id, reservation
                ),
                "published_page_size": len(outputs),
                "has_more_published_outputs": next_cursor is not None,
            }

    artifact_refs = {}
    fetch_refs = set()
    for output in outputs:
        for item in output.artifact_items:
            artifact = item.artifact
            artifact_refs[artifact.artifact_id] = {
                "digest": artifact.digest,
                "size_bytes": artifact.size_bytes,
                "media_type": artifact.media_type,
            }
        payload = getattr(output, "payload", None)
        if isinstance(payload, dict):
            provenance = payload.get("_provenance")
            if isinstance(provenance, dict) and isinstance(
                provenance.get("evidence_ref"), str
            ):
                fetch_refs.add(provenance["evidence_ref"])
    result["published_artifacts"] = [
        {
            "artifact_id": artifact_id,
            **reference,
            "custody": _bc06_metadata(tenant_id, artifact_id),
        }
        for artifact_id, reference in sorted(artifact_refs.items())
    ]
    result["fetch_evidence"] = (
        {"state": "partially_resolved", "references": sorted(fetch_refs)}
        if fetch_refs
        else {
            "state": "unresolved",
            "reason": "BC05 Output does not expose a FetchEvidence index; use Gateway metadata with a known FetchEvidence ID",
        }
    )
    inventory = _bc06_inventory(tenant_id, cursor=custody_cursor, limit=custody_limit)
    if inventory.get("state") is None:
        inventory["state"] = "tenant_page_inspected"
        inventory["referenced_artifact_ids"] = sorted(artifact_refs)
        inventory["unknown_dispositions"] = (
            "unreferenced or incomplete custody remains unknown; no deletion is inferred"
        )
    result["residual_custody"] = inventory
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--custody-limit", type=int, default=50)
    parser.add_argument("--custody-cursor")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--submission-id")
    group.add_argument("--run-id")
    args = parser.parse_args()
    try:
        document = inspect(
            tenant_id=args.tenant_id,
            submission_id=args.submission_id,
            run_id=args.run_id,
            custody_cursor=args.custody_cursor,
            custody_limit=args.custody_limit,
        )
    except Exception as error:  # noqa: BLE001 - CLI reports owner client failures as data.
        print(json.dumps({"state": "inspection_failed", "error": str(error)}))
        return 2
    print(json.dumps(document, sort_keys=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
