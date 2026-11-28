"""Inspect or explicitly release published raw-only Gateway evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys

from acquisition_application.adapters.artifact_custody import ArtifactCustodyHTTPClient
from acquisition_application.adapters.evidence_release_authorization import (
    EvidenceReleaseOperatorAllowlist,
)
from acquisition_application.adapters.gateway_evidence import (
    GatewayEvidenceMetadataHTTPClient,
    GatewayEvidenceReleaseHTTPClient,
)
from acquisition_application.application import TrustedAcquisitionApplication
from acquisition_application.result_bundle import (
    VerifiedFixturePage,
    VerifiedResultBundle,
)
from acquisition_output.adapters.mongodb import MongoOutputStore, MongoResources
from acquisition_output.adapters.mongodb.config import MongoSettings
from acquisition_output.application import AcquisitionOutputApplication
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fetch-id", action="append", required=True)
    parser.add_argument(
        "--release",
        action="store_true",
        help="explicitly perform the eligible body release",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    required = (
        "BC04_DATABASE_URL",
        "ARTIFACT_CUSTODY_HTTP_URL",
        "ARTIFACT_CUSTODY_LOCAL_TOKEN",
        "GATEWAY_HTTP_URL",
        "GATEWAY_EVIDENCE_IMPORT_TOKEN",
        "GATEWAY_EVIDENCE_RELEASE_TOKEN",
        "MOZAIC_OPERATOR_ID",
        "MOZAIC_EVIDENCE_RELEASE_OPERATORS",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print(json.dumps({"state": "blocked", "missing_configuration": missing}))
        return 2
    try:
        repo = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"])
        snapshot = repo.get(args.tenant_id, args.run_id)
        if snapshot is None or snapshot.intent.binding.output_modality != "artifact":
            raise ValueError(
                "operator release CLI supports only an exact raw artifact run"
            )
        if not snapshot.attempt_id or snapshot.generation < 1:
            raise ValueError("BC04 has no exact current attempt for this run")
        metadata_client = GatewayEvidenceMetadataHTTPClient(
            os.environ["GATEWAY_HTTP_URL"], os.environ["GATEWAY_EVIDENCE_IMPORT_TOKEN"]
        )
        pages = []
        for fetch_id in args.fetch_id:
            item = metadata_client.get(
                tenant_id=args.tenant_id,
                run_id=args.run_id,
                attempt_id=snapshot.attempt_id,
                generation=snapshot.generation,
                fetch_id=fetch_id,
            )
            pages.append(
                VerifiedFixturePage(
                    url="",
                    evidence_ref=fetch_id,
                    digest=str(item["body_digest"]),
                    media_type=str(item["media_type"]),
                    body=b"",
                    body_size=int(item["body_size"]),
                )
            )
        bundle = VerifiedResultBundle(
            records=(),
            pages=tuple(pages),
            records_digest="",
            total_bytes=sum(page.acquired_size for page in pages),
            artifact_only=True,
        )
        with MongoResources(MongoSettings.from_env()) as resources:
            store = MongoOutputStore(resources)
            outputs = AcquisitionOutputApplication(
                store, store, payload_contracts=None, custody=None
            )
            application = TrustedAcquisitionApplication(
                repo,
                outputs,
                ArtifactCustodyHTTPClient(
                    os.environ["ARTIFACT_CUSTODY_HTTP_URL"],
                    os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"],
                ),
                evidence_release=GatewayEvidenceReleaseHTTPClient(
                    os.environ["GATEWAY_HTTP_URL"],
                    os.environ["GATEWAY_EVIDENCE_RELEASE_TOKEN"],
                ),
                evidence_release_authorizer=EvidenceReleaseOperatorAllowlist(
                    os.environ["MOZAIC_EVIDENCE_RELEASE_OPERATORS"].split(",")
                ),
            )
            result = application.inspect_evidence_release(
                args.tenant_id, args.run_id, bundle
            )
            if args.release:
                result = application.release_published_evidence(
                    args.tenant_id,
                    args.run_id,
                    bundle,
                    operator_identity=os.environ["MOZAIC_OPERATOR_ID"],
                )
        result["state"] = "released" if args.release else "eligible"
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001 - present owner rejection as operator diagnostics.
        print(json.dumps({"state": "blocked", "reason": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
