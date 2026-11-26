"""Run one compiled Scrapy fixture through durable BC04, BC05 and BC06."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from acquisition_acceptance_support import (
    InProcessBC05Publisher,
    artifact_request,
    key_material,
)
from acquisition_application import (
    ExactJSONSchemaPayloadContractValidator,
    FixtureImportError,
    TrustedAcquisitionApplication,
)
from acquisition_application.adapters.artifact_custody import ArtifactCustodyHTTPClient
from acquisition_application.result_bundle import _strict_json
from acquisition_output.adapters.http_custody import ArtifactCustodyHTTPAdapter
from acquisition_output.adapters.mongodb import MongoOutputStore, MongoResources
from acquisition_output.adapters.mongodb.config import MongoSettings
from acquisition_output.adapters.publication_grant import (
    Ed25519PublicationGrantVerifier,
)
from acquisition_output.application import AcquisitionOutputApplication
from acquisition_output.domain import (
    PayloadContractRef,
    ProducerIdentity,
)
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from execution_coordination.adapters.publication_grant import (
    Ed25519PublicationGrantSigner,
)
from execution_coordination.adapters.scrapy_container import (
    ExecutorConfig,
    SandboxLimits,
    ScrapyContainerExecutor,
)
from execution_coordination.domain import (
    ExecutionReport,
    ExecutionRequest,
    request_identities,
)
from execution_coordination.durable_application import DurableExecutionCoordinator
from execution_coordination.durable_domain import (
    DurableRunIntent,
    FailureDisposition,
)
from jsonschema import Draft202012Validator, FormatChecker
from mozaic_bot_build import canonical_json
from mozaic_contract_resolution import ImmutableContractRegistry
from run_execution_coordination_e2e import _binding, _build_image, _prepare_catalog
from scrapy_collector.catalog.model import load_catalog

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ROOT = ROOT / ".local/execution-coordination-fixture"
ASSEMBLIES = LOCAL_ROOT / "assemblies"
RESULTS = LOCAL_ROOT / "results"
BUILD_SCRATCH = LOCAL_ROOT / "scratch"
PLAYBOOK_ID = "html-simple-catalog"
FIXTURE_REVISION = "fixture-1"
TENANT_ID = "tenant-fixture-acquisition"
PLAYBOOK = ROOT / "examples/playbooks/html-simple"
PAYLOAD_SCHEMA = PLAYBOOK / "output-contract.schema.json"
EXPECTED_RECORDS = PLAYBOOK / "expected-records.jsonl"
EXECUTION_REPORT_SCHEMA = (
    ROOT / "contracts/execution-report/draft/v0/ExecutionReport.schema.json"
)


def _contract_registry() -> ImmutableContractRegistry:
    return ImmutableContractRegistry(ROOT / "contracts/payload/draft/v1")


def _make_app(store: MongoOutputStore, *, contract: PayloadContractRef):
    base_url = os.environ["ARTIFACT_CUSTODY_HTTP_URL"]
    token = os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"]
    _seed, key_id, public = key_material()
    verifier = Ed25519PublicationGrantVerifier({key_id: public})
    return AcquisitionOutputApplication(
        store,
        store,
        payload_contracts=ExactJSONSchemaPayloadContractValidator(_contract_registry()),
        custody=ArtifactCustodyHTTPAdapter(base_url, token),
        outbox=store,
        publication_grants=verifier,
    )


def _contract() -> PayloadContractRef:
    installed = _contract_registry().installed_reference(
        "synthetic.html-simple-catalog.record", "1.0.0"
    )
    return PayloadContractRef(
        installed.contract_id,
        installed.version,
        installed.digest,
    )


def _check_local_fixture_paths() -> None:
    for path in (ROOT / ".local", LOCAL_ROOT, ASSEMBLIES, RESULTS, BUILD_SCRATCH):
        if path.is_symlink():
            raise RuntimeError("local fixture state cannot contain symlink roots")


def _persist_assembly(assembled: Path) -> Path:
    catalog = load_catalog(assembled)
    target = ASSEMBLIES / catalog.assembly_id
    if ASSEMBLIES.is_symlink() or target.is_symlink():
        raise RuntimeError("local assembly cache cannot contain symlinks")
    ASSEMBLIES.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        existing = load_catalog(target)
        if existing.assembly_id != catalog.assembly_id:
            raise RuntimeError("existing local assembly has a different identity")
        if (existing.root / "catalog.json").read_bytes() != (
            catalog.root / "catalog.json"
        ).read_bytes():
            raise RuntimeError("existing local assembly content conflicts")
        return target
    shutil.copytree(assembled, target)
    return target


def _selected_entry(catalog_root: Path, bot_id: str, build_id: str):
    catalog = load_catalog(catalog_root)
    selected = catalog.resolve(bot_id, build_id)
    if (
        selected.playbook_id != PLAYBOOK_ID
        or selected.source.get("source_revision") != FIXTURE_REVISION
    ):
        raise RuntimeError("selected build is not the approved synthetic fixture case")
    return catalog, selected


def _verify_catalog_binding(binding, catalog, selected) -> None:
    manifest_path = catalog.build_root(selected) / "manifest.json"
    manifest = manifest_path.read_bytes()
    fixtures_digest = hashlib.sha256(
        canonical_json(list(selected.input_fixtures))
    ).hexdigest()
    catalog_digest = hashlib.sha256(
        (catalog.root / "catalog.json").read_bytes()
    ).hexdigest()
    if (
        catalog.assembly_id != binding.assembly_id
        or catalog_digest != binding.catalog_digest
        or selected.bot_id != binding.bot_id
        or selected.build_id != binding.build_id
        or selected.build_manifest_digest != binding.bot_build_manifest_digest
        or hashlib.sha256(manifest).hexdigest() != binding.bot_build_manifest_digest
        or selected.source.get("source_id") != binding.source_id
        or selected.source.get("source_revision") != binding.source_revision
        or selected.build_id != binding.fixture_bundle_id
        or fixtures_digest != binding.fixture_bundle_digest
    ):
        raise RuntimeError("immutable BotBuild/catalog binding verification failed")


def _new_execution_report(result_dir: Path, snapshot, binding) -> ExecutionReport:
    data = (result_dir / "execution-report.json").read_bytes()
    report = _strict_json(data, "preserved execution report")
    schema = _strict_json(
        EXECUTION_REPORT_SCHEMA.read_bytes(), "execution report schema"
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(report)
    if (
        report.get("run_id") != snapshot.intent.run_id
        or report.get("attempt_id") != snapshot.attempt_id
        or report.get("identities") != request_identities(binding)
        or report.get("technical_status") != "succeeded"
    ):
        raise RuntimeError("preserved report is not bound to the durable attempt")
    return ExecutionReport(
        contract_version="execution-report/v0",
        request_id=report["request_id"],
        run_id=report["run_id"],
        attempt_id=report["attempt_id"],
        execution_id=report["execution_id"],
        technical_status=report["technical_status"],
        binding=binding,
        duration_ms=report["duration_ms"],
        records_count=report["counts"]["records"],
        output_bytes=report["counts"]["output_bytes"] + len(data),
        output_sha256=report["outputs"]["records_sha256"],
        output_ref=str(result_dir),
    )


def _import_from_disk(tenant_id: str, run_id: str) -> tuple[str, str, object]:
    _check_local_fixture_paths()
    repository = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"])
    snapshot = repository.get(tenant_id, run_id)
    if snapshot is None:
        raise RuntimeError("durable fixture CaptureRun does not exist")
    binding = snapshot.intent.binding
    catalog_root = ASSEMBLIES / binding.assembly_id
    catalog, selected = _selected_entry(catalog_root, binding.bot_id, binding.build_id)
    _verify_catalog_binding(binding, catalog, selected)
    if snapshot.attempt_id is None:
        raise RuntimeError("CaptureRun has no durable attempt")
    result_dir = RESULTS / run_id / snapshot.attempt_id
    report = _new_execution_report(result_dir, snapshot, binding)
    contract = _contract()
    producer = ProducerIdentity(
        producer_id=binding.bot_id,
        producer_version="collector-fixture-v1",
        capability_id="collector.scrapy-fixture",
        capability_version="1.0.0",
        release_id=binding.build_id,
        release_digest="sha256:" + binding.bot_build_manifest_digest,
    )
    with MongoResources(MongoSettings.from_env()) as resources:
        store = MongoOutputStore(resources)
        outputs = _make_app(store, contract=contract)
        importer = TrustedAcquisitionApplication(
            repository,
            outputs,
            ArtifactCustodyHTTPClient(
                os.environ["ARTIFACT_CUSTODY_HTTP_URL"],
                os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"],
            ),
        )
        output_set_id, manifest_digest, bundle = importer.import_fixture_result(
            tenant_id,
            run_id,
            result_dir=result_dir,
            execution_report=report,
            fixture_root=catalog.fixture_root(selected),
            catalog_entry=selected,
            build_root=catalog.build_root(selected),
            execution_report_schema_path=EXECUTION_REPORT_SCHEMA,
            payload_contract=contract,
            producer=producer,
        )
    return output_set_id, manifest_digest, bundle


def _verify_completed_run(
    tenant_id: str, run_id: str, output_set_id: str, bundle
) -> None:
    snapshot = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"]).get(
        tenant_id, run_id
    )
    if snapshot is None or snapshot.state.value != "succeeded":
        raise RuntimeError("BC04 did not record terminal success")
    if (
        snapshot.publication_receipt is None
        or snapshot.reservation is None
        or snapshot.reservation.output_set_id != output_set_id
        or snapshot.publication_receipt.get("manifest_digest")
        != snapshot.reservation.manifest_digest
    ):
        raise RuntimeError("BC04 durable receipt does not match the reservation")
    with MongoResources(MongoSettings.from_env()) as resources:
        store = MongoOutputStore(resources)
        app = _make_app(store, contract=_contract())
        page = app.read_published_outputs(tenant_id, output_set_id, limit=100)
        if len(page.outputs) != len(bundle.records) or page.next_cursor is not None:
            raise RuntimeError("published output page differs from fixture records")
        if any(output.output_type != "composite" for output in page.outputs):
            raise RuntimeError("fixture records were not published as composites")
        artifact_refs = {
            item.artifact.artifact_id: item.artifact
            for output in page.outputs
            for item in output.artifact_items
        }
        expected_pages = {
            page.digest.removeprefix("sha256:"): page.body for page in bundle.pages
        }
        if not artifact_refs or any(
            ref.digest.removeprefix("sha256:") not in expected_pages
            for ref in artifact_refs.values()
        ):
            raise RuntimeError(
                "published outputs do not reference expected fixture bytes"
            )
        for artifact in artifact_refs.values():
            content = artifact_request(tenant_id, artifact.artifact_id)
            if (
                len(content) != artifact.size_bytes
                or hashlib.sha256(content).hexdigest()
                != artifact.digest.removeprefix("sha256:")
                or content != expected_pages[artifact.digest.removeprefix("sha256:")]
            ):
                raise RuntimeError("BC06 artifact bytes differ from fixture evidence")


def _replay_child(run_id: str) -> None:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--replay-import",
        run_id,
        "--tenant-id",
        TENANT_ID,
    ]
    env = os.environ.copy()
    env["BC04_LOCAL_FIXTURE_PROFILE"] = "1"
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, env=env, timeout=90
    )
    if completed.returncode:
        raise RuntimeError(
            f"fresh-process importer replay failed: {completed.stderr[-2000:]}"
        )


def _reconcile_child(tenant_id: str, run_id: str) -> None:
    command = [
        sys.executable,
        str(ROOT / "tooling/execution_coordination_reconcile.py"),
        "--limit",
        "1",
        "--timeout-seconds",
        "60",
        "--tenant-id",
        tenant_id,
        "--run-id",
        run_id,
    ]
    env = os.environ.copy()
    env["BC04_LOCAL_FIXTURE_PROFILE"] = "1"
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, env=env, timeout=90
    )
    if completed.returncode:
        raise RuntimeError(
            f"fresh-process finalization reconciliation failed: {completed.stderr[-2000:]}"
        )


def _run_new_fixture() -> None:
    if os.name != "posix":
        raise RuntimeError("fixture acquisition requires a POSIX host and Docker")
    if os.environ.get("BC04_LOCAL_FIXTURE_PROFILE") != "1":
        raise RuntimeError("explicit local fixture authorization profile is required")
    _check_local_fixture_paths()
    RESULTS.mkdir(parents=True, exist_ok=True, mode=0o700)
    signing_seed, key_id, _public_key = key_material()
    token = os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"]
    _check_local_fixture_paths()
    BUILD_SCRATCH.mkdir(parents=True, exist_ok=True, mode=0o700)
    if BUILD_SCRATCH.is_symlink():
        raise RuntimeError("fixture build scratch cannot be a symlink")
    with tempfile.TemporaryDirectory(
        prefix="dm-durable-fixture-", dir=BUILD_SCRATCH
    ) as temporary:
        temp = Path(temporary)
        assembled, entries, _ = _prepare_catalog(temp / "catalog", "linux/amd64")
        catalog = load_catalog(assembled)
        entry = next(
            selected
            for selected in (
                catalog.resolve(item["bot_id"], item["build_id"]) for item in entries
            )
            if selected.playbook_id == PLAYBOOK_ID
            and selected.source.get("source_revision") == FIXTURE_REVISION
        )
        tag: str | None = None
        try:
            tag, image_ref, image_id = _build_image(assembled, temp)
            contract = _contract()
            binding = _binding(
                assembled,
                image_ref,
                image_id,
                {"bot_id": entry.bot_id, "build_id": entry.build_id},
                60,
            )
            binding = replace(
                binding,
                payload_contract_id=contract.contract_id,
                payload_contract_version=contract.version,
                payload_contract_digest=contract.digest,
            )
            persistent_assembly = _persist_assembly(assembled)
            _selected_catalog, selected = _selected_entry(
                persistent_assembly, entry.bot_id, entry.build_id
            )
            _verify_catalog_binding(binding, _selected_catalog, selected)

            run_id = str(uuid.uuid4())
            request_id = "fixture-request-" + uuid.uuid4().hex
            run_deadline = datetime.now(UTC) + timedelta(minutes=3)
            repository = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"])
            with MongoResources(MongoSettings.from_env()) as resources:
                store = MongoOutputStore(resources)
                app = _make_app(store, contract=contract)
                coordinator = DurableExecutionCoordinator(
                    repository,
                    InProcessBC05Publisher(app),
                    Ed25519PublicationGrantSigner(signing_seed, key_id=key_id),
                    lease_seconds=120,
                )
                coordinator.create_run(
                    DurableRunIntent(
                        TENANT_ID,
                        run_id,
                        request_id,
                        "fixture-idempotency-" + run_id,
                        binding,
                        run_deadline,
                        max_attempts=2,
                    )
                )
                lease = coordinator.acquire_attempt(
                    TENANT_ID,
                    run_id,
                    "protected-scrapy-fixture-worker",
                    "claim-fixture-" + run_id,
                )
                attempt_deadline = min(run_deadline, lease.lease_until)
                worker_request = ExecutionRequest(
                    "execution-request/v0",
                    request_id,
                    run_id,
                    lease.attempt_id,
                    binding,
                    attempt_deadline,
                    correlation={"correlation_id": request_id},
                )
                executor = ScrapyContainerExecutor(
                    ExecutorConfig(
                        trusted_bindings={(entry.bot_id, entry.build_id): binding},
                        output_root=RESULTS,
                        limits=SandboxLimits(
                            cpus=1.0,
                            memory_bytes=536_870_912,
                            pids=64,
                        ),
                    )
                )
                report: ExecutionReport | None = None
                try:
                    execution = executor.start(worker_request)
                    report = execution.wait(attempt_deadline)
                    if report.technical_status != "succeeded":
                        executor.discard(report)
                        coordinator.fail_attempt(
                            lease,
                            FailureDisposition.PERMANENT,
                            "FIXTURE_WORKER_FAILED",
                            "fixture-worker-failed-" + run_id,
                        )
                        raise RuntimeError(
                            f"protected fixture worker ended as {report.technical_status}"
                        )
                    report = executor.publish(report)
                except Exception as error:
                    if report is None:
                        try:
                            coordinator.fail_attempt(
                                lease,
                                FailureDisposition.PERMANENT,
                                "FIXTURE_LAUNCH_FAILED",
                                "fixture-launch-failed-" + run_id,
                            )
                        except Exception as transition_error:  # noqa: BLE001
                            error.add_note(
                                "BC04 could not persist launch failure: "
                                + type(transition_error).__name__
                            )
                    raise
                fixture_root = load_catalog(persistent_assembly).fixture_root(selected)
                importer = TrustedAcquisitionApplication(
                    repository,
                    app,
                    ArtifactCustodyHTTPClient(
                        os.environ["ARTIFACT_CUSTODY_HTTP_URL"], token
                    ),
                    finalization=coordinator,
                )
                bundle = importer.verify_fixture_result(
                    TENANT_ID,
                    run_id,
                    result_dir=Path(report.output_ref),
                    execution_report=report,
                    fixture_root=fixture_root,
                    catalog_entry=selected,
                    build_root=catalog.build_root(selected),
                    execution_report_schema_path=EXECUTION_REPORT_SCHEMA,
                )
                expected_records = [
                    _strict_json(line, "fixture test oracle")
                    for line in EXPECTED_RECORDS.read_bytes().splitlines()
                    if line
                ]
                if list(bundle.records) != expected_records:
                    raise RuntimeError("fixture output differs from independent oracle")
                expected_output_set_id = (
                    "set-"
                    + hashlib.sha256(
                        canonical_json(
                            {
                                "tenant": TENANT_ID,
                                "run": run_id,
                                "attempt": lease.attempt_id,
                                "records": bundle.records_digest,
                            }
                        )
                    ).hexdigest()
                )
                for invalid_report in (
                    replace(report, output_sha256="0" * 64),
                    replace(report, attempt_id=str(uuid.uuid4())),
                ):
                    try:
                        importer.verify_fixture_result(
                            TENANT_ID,
                            run_id,
                            result_dir=Path(report.output_ref),
                            execution_report=invalid_report,
                            fixture_root=fixture_root,
                            catalog_entry=selected,
                            build_root=catalog.build_root(selected),
                            execution_report_schema_path=EXECUTION_REPORT_SCHEMA,
                        )
                    except FixtureImportError:
                        pass
                    else:
                        raise RuntimeError(
                            "invalid or stale worker result was accepted"
                        )
                    if app.inspect_output_set(TENANT_ID, expected_output_set_id):
                        raise RuntimeError(
                            "rejected worker result changed BC05 persistence"
                        )
                producer = ProducerIdentity(
                    binding.bot_id,
                    "collector-fixture-v1",
                    "collector.scrapy-fixture",
                    "1.0.0",
                    binding.build_id,
                    "sha256:" + binding.bot_build_manifest_digest,
                )
                output_set_id, manifest_digest = importer.import_verified_result(
                    TENANT_ID,
                    run_id,
                    bundle=bundle,
                    producer=producer,
                    payload_contract=contract,
                )
                replay_set_id, replay_manifest = importer.import_verified_result(
                    TENANT_ID,
                    run_id,
                    bundle=bundle,
                    producer=producer,
                    payload_contract=contract,
                )
                if (replay_set_id, replay_manifest) != (output_set_id, manifest_digest):
                    raise RuntimeError(
                        "import replay changed OutputSet identity or manifest"
                    )
                lost_ack = DurableExecutionCoordinator(
                    repository,
                    InProcessBC05Publisher(app, lose_ack=True),
                    Ed25519PublicationGrantSigner(signing_seed, key_id=key_id),
                    lease_seconds=120,
                )
                try:
                    TrustedAcquisitionApplication(
                        repository,
                        app,
                        ArtifactCustodyHTTPClient(
                            os.environ["ARTIFACT_CUSTODY_HTTP_URL"], token
                        ),
                        finalization=lost_ack,
                    ).finalize_output_set(lease, output_set_id, manifest_digest)
                except TimeoutError:
                    pass
                else:
                    raise RuntimeError("lost-ack recovery scenario was not exercised")
                pending = repository.get(TENANT_ID, run_id)
                if pending is None or pending.state.value != "finalization_pending":
                    raise RuntimeError(
                        "lost acknowledgment did not preserve reservation"
                    )
                _reconcile_child(TENANT_ID, run_id)

            _replay_child(run_id)
            _verify_completed_run(TENANT_ID, run_id, output_set_id, bundle)
            print(
                "Durable Scrapy fixture acquisition passed: "
                f"run={run_id} output_set={output_set_id} outputs={len(bundle.records)} "
                f"fixture_pages={len(bundle.pages)} manifest={manifest_digest} "
                f"payload_contract={contract.contract_id}@{contract.version}#{contract.digest} "
                "network=none replay=fresh-process publication=BC04-authorized"
            )
        finally:
            if tag:
                subprocess.run(
                    ["docker", "image", "rm", "--force", tag],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-import")
    parser.add_argument("--tenant-id")
    args = parser.parse_args()
    if args.replay_import:
        if args.tenant_id != TENANT_ID:
            parser.error("--tenant-id must match the fixture profile tenant")
        output_set_id, manifest_digest, _bundle = _import_from_disk(
            args.tenant_id, args.replay_import
        )
        print(
            f"Replayed fixture import from persisted result bundle: "
            f"run={args.replay_import} output_set={output_set_id} "
            f"manifest={manifest_digest}"
        )
        return 0
    if args.tenant_id is not None:
        parser.error("--tenant-id is only valid with --replay-import")
    _run_new_fixture()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
