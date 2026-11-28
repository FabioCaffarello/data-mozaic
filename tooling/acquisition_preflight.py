"""Bounded, read-only preflight across acquisition owner health interfaces."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import psycopg
from acquisition_application.adapters.artifact_custody import ArtifactCustodyHTTPClient
from acquisition_output.adapters.mongodb import MongoResources
from acquisition_output.adapters.mongodb.config import MongoSettings
from mozaic_contract_resolution import ContractReference, ImmutableContractRegistry


def _http(
    url: str, *, token: str | None = None, maximum: int = 8192
) -> dict[str, object]:
    headers = {"Authorization": "Bearer " + token} if token else {}
    request = Request(url, headers=headers)
    with urlopen(request, timeout=3) as response:
        raw = response.read(maximum + 1)
        if len(raw) > maximum or response.read(1):
            raise ValueError("response exceeded its bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("owner response was not an object")
    return value


def _check(checks: dict[str, object], name: str, operation) -> None:
    try:
        value = operation()
        checks[name] = {"state": "ready", "detail": value}
    except Exception as error:  # noqa: BLE001 - preflight reports owner status without hiding failures.
        checks[name] = {
            "state": "blocked",
            "reason": type(error).__name__,
            "detail": str(error),
        }


def preflight(release_id: str | None) -> dict[str, object]:
    checks: dict[str, object] = {}
    gateway = os.environ.get("GATEWAY_HTTP_URL", "http://127.0.0.1:8090").rstrip("/")
    custody = os.environ.get("ARTIFACT_CUSTODY_HTTP_URL", "").rstrip("/")
    _check(
        checks,
        "gateway_connectivity",
        lambda: (
            _http(gateway + "/healthz")
            if gateway
            else (_ for _ in ()).throw(ValueError("GATEWAY_HTTP_URL is required"))
        ),
    )
    _check(
        checks,
        "bc06_connectivity",
        lambda: (
            _http(custody + "/healthz")
            if custody
            else (_ for _ in ()).throw(
                ValueError("ARTIFACT_CUSTODY_HTTP_URL is required")
            )
        ),
    )

    def resource_capacity():
        if not gateway:
            raise ValueError("GATEWAY_HTTP_URL is required")
        token = os.environ.get("GATEWAY_EVIDENCE_IMPORT_TOKEN")
        if not token:
            raise ValueError("GATEWAY_EVIDENCE_IMPORT_TOKEN is required")
        current = _http(gateway + "/internal/v1/resources", token=token)
        available_bytes = (
            current["spool_max_bytes"]
            - current["spool_bytes_used"]
            - current["spool_bytes_reserved"]
        )
        available_entries = (
            current["spool_max_entries"]
            - current["spool_entries"]
            - current["spool_entry_reservations"]
        )
        archive_available = current.get("spool_archive_bytes_available")
        archive_exhausted = current.get("spool_archive_exhausted")
        if (
            available_bytes <= 0
            or available_entries <= 0
            or archive_available is None
            or archive_exhausted is None
        ):
            raise RuntimeError("Gateway resource snapshot is incomplete or exhausted")
        if archive_exhausted or archive_available <= 0:
            raise RuntimeError("Gateway historical evidence archive is exhausted")
        return {
            "available_bytes": available_bytes,
            "available_entries": available_entries,
            "archive_available_bytes": archive_available,
            "archive_exhausted": archive_exhausted,
        }

    _check(checks, "gateway_capacity", resource_capacity)

    for name, env_name, schema in (
        ("bc04_database", "BC04_DATABASE_URL", "execution_coordination"),
        ("bc13_database", "WORK_INTAKE_DATABASE_URL", "work_intake"),
    ):

        def database_check(env_name=env_name, schema=schema):
            dsn = os.environ[env_name]
            with psycopg.connect(dsn, connect_timeout=3) as connection:
                connection.execute("SELECT 1").fetchone()
                row = connection.execute(
                    "SELECT to_regclass(%s)", (schema + ".schema_migrations",)
                ).fetchone()
                if row is None or row[0] is None:
                    raise RuntimeError("owner migrations are not installed")
                return {"schema": schema, "migrations": "available"}

        _check(checks, name, database_check)

    def bc05_check():
        with MongoResources(MongoSettings.from_env()) as resources:
            resources.client.admin.command("ping")
        return {"ping": "ok", "scope": "connectivity only"}

    _check(checks, "bc05_connectivity", bc05_check)

    def bc06_persistence_check():
        if not custody:
            raise ValueError("ARTIFACT_CUSTODY_HTTP_URL is required")
        client = ArtifactCustodyHTTPClient(
            custody, os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"]
        )
        page = client.list_inventory(
            tenant_id=os.environ.get("MOZAIC_PREFLIGHT_TENANT_ID", "preflight-tenant"),
            limit=1,
        )
        return {"state": "queryable", "page_size": len(page["artifacts"])}

    _check(checks, "bc06_persistence", bc06_persistence_check)

    operator_identity = os.environ.get("MOZAIC_OPERATOR_ID", "")
    operator_allowlist = {
        value.strip()
        for value in os.environ.get("MOZAIC_EVIDENCE_RELEASE_OPERATORS", "").split(",")
        if value.strip()
    }
    identity_ready = bool(operator_identity and operator_identity in operator_allowlist)
    release_credential = bool(os.environ.get("GATEWAY_EVIDENCE_RELEASE_TOKEN"))
    checks["operator_identity"] = {
        "state": "ready" if identity_ready else "blocked",
        "detail": "configured and authorized"
        if identity_ready
        else "MOZAIC_OPERATOR_ID is missing or absent from MOZAIC_EVIDENCE_RELEASE_OPERATORS",
    }
    checks["evidence_release_authority"] = {
        "state": "ready" if release_credential else "blocked",
        "detail": "separate Gateway release credential configured"
        if release_credential
        else "GATEWAY_EVIDENCE_RELEASE_TOKEN is missing",
    }

    if release_id:
        command = [
            sys.executable,
            "-m",
            "acquisition_lifecycle.cli",
            "resolve-release",
            "--release-id",
            release_id,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=10, check=True
            )
            release = json.loads(result.stdout)
            state = release.get("state")
            approved = release.get("approved_release")
            checks["requested_release"] = {
                "state": "ready"
                if state == "approved" and isinstance(approved, dict)
                else "blocked",
                "detail": {"release_id": release_id, "state": state},
            }
            reference = (
                approved.get("input_contract") if isinstance(approved, dict) else None
            )
            if isinstance(reference, dict):
                try:
                    ImmutableContractRegistry(
                        Path(
                            os.environ.get(
                                "MOZAIC_PREFLIGHT_CONTRACT_ROOT",
                                os.environ.get(
                                    "MOZAIC_PAYLOAD_CONTRACT_ROOT",
                                    "contracts/payload/draft/v1",
                                ),
                            )
                        )
                    ).resolve(
                        ContractReference(
                            reference["contract_id"],
                            reference["version"],
                            reference["digest"],
                        )
                    )
                    checks["contract_availability"] = {
                        "state": "ready",
                        "detail": reference,
                    }
                except Exception as error:  # noqa: BLE001
                    checks["contract_availability"] = {
                        "state": "blocked",
                        "reason": type(error).__name__,
                    }
            else:
                checks["contract_availability"] = {
                    "state": "unknown",
                    "detail": "release has no Input Contract",
                }
        except Exception as error:  # noqa: BLE001
            checks["requested_release"] = {
                "state": "blocked",
                "reason": type(error).__name__,
                "detail": str(error),
            }
            checks["contract_availability"] = {
                "state": "unknown",
                "detail": "release could not be resolved",
            }
    else:
        checks["requested_release"] = {
            "state": "unknown",
            "detail": "pass --release-id for acquisition-specific readiness",
        }
        checks["contract_availability"] = {
            "state": "unknown",
            "detail": "release-specific check not requested",
        }
    blocking = [name for name, value in checks.items() if value.get("state") != "ready"]
    return {
        "contract_version": "acquisition-preflight/v1",
        "mode": "specific_acquisition" if release_id else "development_environment",
        "advisory": True,
        "capacity_reserved": False,
        "protected_scout_readiness": "separate_capability; not evaluated",
        "checks": checks,
        "blocking_or_unknown": blocking,
        "state": "ready" if not blocking else "not_ready",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-id")
    args = parser.parse_args()
    result = preflight(args.release_id)
    print(json.dumps(result, sort_keys=True, indent=2, default=str))
    return 0 if result["state"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
