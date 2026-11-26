"""Run the controlled live JSON flow through BC04, Scrapy, gateway, BC06 and BC05."""

from __future__ import annotations

import argparse
import atexit
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import rfc8785
from acquisition_acceptance_support import (
    InProcessBC05Publisher,
    artifact_request,
    configuration,
    key_material,
    write,
)
from acquisition_application import (
    ExactJSONSchemaPayloadContractValidator,
    FixtureImportError,
    TrustedAcquisitionApplication,
    stable_identifier,
)
from acquisition_application.adapters.artifact_custody import ArtifactCustodyHTTPClient
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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from execution_coordination.adapters.acquisition_authorization_http import (
    acquisition_authorization_handler,
)
from execution_coordination.adapters.execution_capability import (
    ExecutionCapabilitySigner,
)
from execution_coordination.adapters.postgres_durable import (
    PostgresDurableRunRepository,
)
from execution_coordination.adapters.publication_grant import (
    Ed25519PublicationGrantSigner,
)
from execution_coordination.adapters.trusted_live_authority import TrustedLiveAuthority
from execution_coordination.durable_application import DurableExecutionCoordinator
from execution_coordination.durable_domain import FailureDisposition
from mozaic_bot_build import canonical_json, verify_bot_build
from mozaic_contract_resolution import ContractReference, ImmutableContractRegistry

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deployments/compose/acquisition-live.compose.yaml"
TENANT = "tenant-controlled-live-json"
SOURCE_ID = "controlled-http"
REVISION_ID = "controlled-v1"
POLICY_ID = "controlled-live-json-get"
POLICY_VERSION = "1.0.0"
AUTHORITY_ID = "local-controlled-live-authority"
AUDIENCE = "data-mozaic/acquisition-gateway"
PAYLOAD_SCHEMA = (
    ROOT / "examples/use-cases/controlled-live-json/output-contract.schema.json"
)
EXPECTED = ROOT / "examples/use-cases/controlled-live-json/expected-records.jsonl"
CASE_KIND = "json"
MEDIA_TYPE = "application/json"
SOURCE_PATH = "/v1/items"
SOURCE_PATHS = ("/v1/items",)
SOURCE_PATH_PREFIXES = SOURCE_PATHS
PAGINATION_IMPORT_KWARGS: dict[str, object] = {}
REDIRECT_IMPORT_KWARGS: dict[str, object] = {}
PLAYBOOK = ROOT / "examples/use-cases/controlled-live-json/capture-playbook.yaml"
FIXTURES = ROOT / "examples/use-cases/controlled-live-json/fixtures"
FAILURE_CASES = {
    "oversized",
    "slow",
    "malformed",
    "invalid-output",
    "interrupted-body",
    "pagination-incomplete",
}
FAILURE_DIAGNOSTICS = {
    "oversized": ("response_too_large", "GATEWAY_RESPONSE_TOO_LARGE"),
    "slow": (
        "upstream_request_failed",
        "GATEWAY_UNAVAILABLE",
        "deadline",
        "timeout",
    ),
    "malformed": ("Expecting value", "JSONDecodeError", "JSONDecode"),
    "invalid-output": ("FIELD_CONVERSION_INVALID",),
    "interrupted-body": (
        "response_interrupted",
        "GATEWAY_UNAVAILABLE",
        "ClientPayloadError",
    ),
    "pagination-incomplete": ("PAGINATION_UNEXPECTED_BUDGET_EXHAUSTED",),
}


class _InjectedImportInterruption(RuntimeError):
    pass


class _ImportInterruptionProxy:
    """Harness-only failures at BC05's trusted importer boundary."""

    def __init__(self, application, mode: str):
        self._application = application
        self._mode = mode
        self.output_set_id: str | None = None
        self.staged = 0
        self.stage_attempts = 0

    def __getattr__(self, name):
        return getattr(self._application, name)

    def create_output_set(self, spec, operation):
        self.output_set_id = spec.output_set_id
        if self._mode == "custody-before-stage":
            raise _InjectedImportInterruption("injected-after-custody-before-set")
        return self._application.create_output_set(spec, operation)

    def stage_output(self, output):
        self.stage_attempts += 1
        if self._mode == "partial-stage" and self.stage_attempts == 2:
            raise _InjectedImportInterruption("injected-during-output-staging")
        result = self._application.stage_output(output)
        self.staged += 1
        return result


def _configure_case(case: str) -> None:
    global CASE_KIND, TENANT, POLICY_ID, PAYLOAD_SCHEMA, EXPECTED
    global REVISION_ID
    global MEDIA_TYPE, SOURCE_PATH, SOURCE_PATHS, SOURCE_PATH_PREFIXES
    global PAGINATION_IMPORT_KWARGS
    global REDIRECT_IMPORT_KWARGS
    global PLAYBOOK, FIXTURES
    if case not in {
        "json",
        "html",
        "pagination",
        "pagination-planned",
        "redirect",
        "redirect-denied",
        "redirect-budget",
        "ack-loss",
        "pagination-cancel",
        "gateway-restart",
        "custody-interruption",
        "partial-stage-interruption",
        "work-intake",
        *FAILURE_CASES,
    }:
        raise ValueError("controlled case must be json, html or pagination variant")
    CASE_KIND = case
    REVISION_ID = (
        "controlled-work-intake-v1" if case == "work-intake" else "controlled-v1"
    )
    TENANT = (
        "tenant-work-intake-m5"
        if case == "work-intake"
        else "tenant-controlled-live-" + case
    )
    POLICY_ID = "controlled-live-" + case + "-get"
    use_case_name = (
        "controlled-live-json"
        if case
        in {
            "ack-loss",
            "custody-interruption",
            "partial-stage-interruption",
            "oversized",
            "slow",
            "malformed",
            "invalid-output",
            "interrupted-body",
            "work-intake",
            *FAILURE_CASES - {"pagination-incomplete"},
        }
        else "controlled-live-pagination-planned"
        if case == "pagination-incomplete"
        else "controlled-live-pagination"
        if case in {"pagination-cancel", "gateway-restart"}
        else "controlled-live-redirect-budget"
        if case == "redirect-budget"
        else "controlled-live-redirect"
        if case in {"redirect", "redirect-denied", "redirect-budget"}
        else "controlled-live-" + case
    )
    use_case = ROOT / "examples/use-cases" / use_case_name
    PAYLOAD_SCHEMA = use_case / "output-contract.schema.json"
    EXPECTED = use_case / "expected-records.jsonl"
    PLAYBOOK = (
        ROOT / "examples/use-cases/controlled-work-intake/capture-playbook.yaml"
        if case == "work-intake"
        else use_case / "capture-playbook.yaml"
    )
    FIXTURES = (
        ROOT / "examples/use-cases/controlled-work-intake/fixtures"
        if case == "work-intake"
        else use_case / "fixtures"
    )
    MEDIA_TYPE = (
        "text/html"
        if case in {"html", "pagination", "pagination-planned", "pagination-incomplete"}
        else "application/json"
    )
    SOURCE_PATHS = (
        ("/v1/window/1", "/v1/window/2")
        if case in {"pagination-planned", "pagination-incomplete"}
        else ("/v1/redirect-chain/0",)
        if case == "redirect-budget"
        else ("/v1/redirect",)
        if case in {"redirect", "redirect-denied"}
        else ("/v1/pages/1", "/v1/pages/2")
        if case in {"pagination", "pagination-cancel", "gateway-restart"}
        else ("/v1/html",)
        if case == "html"
        else ("/v1/oversized",)
        if case == "oversized"
        else ("/v1/slow",)
        if case == "slow"
        else ("/v1/malformed",)
        if case == "malformed"
        else ("/v1/invalid-output",)
        if case == "invalid-output"
        else ("/v1/interrupted-body",)
        if case == "interrupted-body"
        else ("/v1/items",)
    )
    SOURCE_PATH = SOURCE_PATHS[0]
    SOURCE_PATH_PREFIXES = (
        ("/v1/window/",)
        if case == "pagination-planned"
        else ("/v1/redirect-chain",)
        if case == "redirect-budget"
        else ("/v1/redirect", "/v1/items")
        if case in {"redirect", "redirect-denied"}
        else ("/v1/window/",)
        if case == "pagination-incomplete"
        else SOURCE_PATHS
    )
    PAGINATION_IMPORT_KWARGS = (
        {
            "authorized_pagination_mode": (
                "bounded_window" if case == "pagination-planned" else "natural_end"
            ),
            "authorized_max_pages": 2 if case == "pagination-planned" else None,
            "authorized_origin": "https://api.fixture.test",
            "authorized_path_prefixes": SOURCE_PATH_PREFIXES,
        }
        if case
        in {
            "pagination",
            "pagination-planned",
            "pagination-cancel",
            "pagination-incomplete",
            "gateway-restart",
        }
        else {}
    )
    REDIRECT_IMPORT_KWARGS = (
        {
            "expected_effective_paths": ("/v1/items",),
            "expected_redirect_counts": (1,),
        }
        if case == "redirect"
        else {}
    )


def _run(
    args: list[str], *, env: dict[str, str], timeout: int = 300, capture: bool = False
):
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        check=False,
        timeout=timeout,
        capture_output=capture,
        text=True,
    )
    if result.returncode:
        detail = (
            ((result.stderr or "") + "\n" + (result.stdout or ""))[-5000:]
            if capture
            else ""
        )
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n{detail}"
        )
    return result


def _worker_failure_diagnostic(error: RuntimeError) -> str | None:
    """Read the generated worker's final status record from captured output."""
    detail = str(error)
    for line in reversed(detail.splitlines()):
        marker = line.find('{"diagnostic":')
        if marker < 0:
            continue
        try:
            report = json.loads(line[marker:])
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict) and report.get("state") == "failed":
            diagnostic = report.get("diagnostic")
            return diagnostic if isinstance(diagnostic, str) else None
    return None


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _publishable(document: dict[str, object], field: str) -> dict[str, object]:
    document[field] = _digest(
        {key: value for key, value in document.items() if key != field}
    )
    return document


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", 0))
        return int(listener.getsockname()[1])


def _contract_registry() -> ImmutableContractRegistry:
    return ImmutableContractRegistry(ROOT / "contracts/payload/draft/v1")


def _payload_contract(
    registry: ImmutableContractRegistry,
) -> PayloadContractRef:
    contract_id = (
        "controlled-work-intake.record"
        if CASE_KIND == "work-intake"
        else "controlled-live-html.record"
        if CASE_KIND == "html"
        else "controlled-live-pagination.record"
        if CASE_KIND.startswith("pagination")
        else "controlled-live-json.record"
    )
    installed = registry.installed_reference(contract_id, "1.0.0")
    return PayloadContractRef(
        installed.contract_id, installed.version, installed.digest
    )


def _application(
    store: MongoOutputStore, registry: ImmutableContractRegistry
) -> AcquisitionOutputApplication:
    seed, key_id, _ = key_material()
    public = (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return AcquisitionOutputApplication(
        store,
        store,
        payload_contracts=ExactJSONSchemaPayloadContractValidator(registry),
        custody=ArtifactCustodyHTTPAdapter(
            os.environ["ARTIFACT_CUSTODY_HTTP_URL"],
            os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"],
        ),
        outbox=store,
        publication_grants=Ed25519PublicationGrantVerifier({key_id: public}),
    )


def _authority_snapshot(
    policy: dict[str, object],
    build: dict[str, object],
    build_directory: Path,
    playbook_path: Path,
    runtime_digest: str,
    release_id: str,
    generation: int,
    input_contract: ContractReference | None = None,
) -> tuple[bytes, dict[str, bytes]]:
    if os.environ.get("MOZAIC_LIFECYCLE_ACCEPTANCE") == "1":
        from acquisition_lifecycle_acceptance_support import publish_controlled_release
        from mozaic_contract_resolution import ContractReference

        contract = _payload_contract(_contract_registry())
        return publish_controlled_release(
            root=ROOT,
            source_id=SOURCE_ID,
            revision_id=REVISION_ID,
            source_path_prefixes=tuple(SOURCE_PATH_PREFIXES),
            policy=policy,
            payload_contract=ContractReference(
                contract.contract_id, contract.version, contract.digest
            ),
            build_manifest=build,
            build_directory=build_directory,
            playbook_path=playbook_path,
            runtime_digest=runtime_digest,
            input_contract=input_contract,
            release_id=release_id,
            expected_generation=generation,
            authority_id=AUTHORITY_ID,
        )
    now = datetime.now(UTC)
    contract = _payload_contract(_contract_registry())
    source = _publishable(
        {
            "contract_version": "source-revision/v1",
            "source_id": SOURCE_ID,
            "revision_id": REVISION_ID,
            "owner": "Data Mozaic controlled test infrastructure",
            "status": "eligible",
            "scope": {
                "origins": ["https://api.fixture.test"],
                "methods": ["GET"],
                "path_prefixes": list(SOURCE_PATH_PREFIXES),
                "query_parameter_values": {},
            },
            "configuration_version": "1.0.0",
            "configuration_digest": "",
            "metadata": {"classification": "controlled-test-only"},
        },
        "configuration_digest",
    )
    release = _publishable(
        {
            "contract_version": "bot-release/v2",
            "release_id": release_id,
            "status": "approved",
            "bot_id": build["bot_id"],
            "build_id": build["build_id"],
            "build_manifest_digest": "sha256:"
            + hashlib.sha256(canonical_json(build)).hexdigest(),
            "backend_id": "scrapy-live-http",
            "backend_version": str(build["backend_version"]),
            "runtime_profile_id": "scrapy-live-http-v1",
            "runtime_image_digest": runtime_digest,
            "source_id": SOURCE_ID,
            "source_revision_id": REVISION_ID,
            "policy_id": POLICY_ID,
            "policy_version": POLICY_VERSION,
            "compiler_compatibility": "scrapy-live-http/v1",
            "approved_at": now.isoformat(),
            "approval_evidence_ref": "operator://controlled-live-"
            + CASE_KIND
            + "/approval",
            "payload_contract": {
                "contract_id": contract.contract_id,
                "version": contract.version,
                "digest": contract.digest,
            },
        },
        "release_digest",
    )
    snapshot: dict[str, object] = {
        "contract_version": "live-authority-snapshot/v1",
        "authority_id": AUTHORITY_ID,
        "generation": generation,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
        "source_revisions": [source],
        "policies": [policy],
        "releases": [release],
    }
    snapshot["snapshot_digest"] = _digest(snapshot)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    signature = (
        base64.urlsafe_b64encode(key.sign(rfc8785.dumps(snapshot))).decode().rstrip("=")
    )
    envelope = {
        "snapshot": snapshot,
        "key_id": "controlled-authority-key-v1",
        "signature": signature,
    }
    trusted = {"controlled-authority-key-v1": public}
    return json.dumps(envelope, separators=(",", ":")).encode(), trusted


def _policy() -> dict[str, object]:
    policy: dict[str, object] = {
        "contract_version": "acquisition-policy/v1",
        "policy_id": POLICY_ID,
        "policy_version": POLICY_VERSION,
        "source_id": SOURCE_ID,
        "source_revision_id": REVISION_ID,
        "status": "eligible",
        "origins": ["https://api.fixture.test"],
        "methods": ["GET"],
        "path_prefixes": list(SOURCE_PATH_PREFIXES),
        "query_parameter_values": {},
        "dns_policy": "test-fixture-only",
        "test_destination_cidrs": ["172.30.249.10/32"],
        "redirect_policy": (
            "revalidate-every-hop"
            if CASE_KIND in {"redirect", "redirect-budget"}
            else "deny"
        ),
        "max_requests": 4
        if CASE_KIND == "redirect-budget"
        else 2
        if CASE_KIND in {"redirect", "redirect-denied"}
        else len(SOURCE_PATHS),
        "max_concurrency": 1,
        "requests_per_second": 10,
        "max_response_bytes": 100_000,
        "max_total_response_bytes": 200_000,
        "deadline_seconds": 15 if CASE_KIND == "slow" else 90,
        "require_tls": False,
        "credential_refs": [],
        "evidence_retention_policy_ref": "controlled-test-retention-v1",
    }
    if CASE_KIND == "work-intake":
        policy["query_parameter_values"] = {"limit": ["1", "2", "3"]}
    return _publishable(policy, "policy_digest")


def _verify_outputs(
    tenant: str,
    run_id: str,
    output_set_id: str,
    expected_records: int,
    contract: PayloadContractRef,
    *,
    expected_payloads: list[dict[str, object]] | None = None,
) -> tuple[list[str], str, list[str], list[str]]:
    with MongoResources(MongoSettings.from_env()) as resources:
        app = _application(MongoOutputStore(resources), contract)
        page = app.read_published_outputs(tenant, output_set_id, limit=100)
        if len(page.outputs) != expected_records or page.next_cursor is not None:
            raise RuntimeError(
                "independent BC05 read differs from controlled JSON records"
            )
        output_ids = [output.identity.output_id for output in page.outputs]
        observation_ids = [output.identity.observation_id for output in page.outputs]
        if (
            len(set(output_ids)) != expected_records
            or len(set(observation_ids)) != expected_records
        ):
            raise RuntimeError("published replay duplicated an output or observation")
        if any(output.output_type != "composite" for output in page.outputs):
            raise RuntimeError("published live records are not composite outputs")
        if any(output.output_set_id != output_set_id for output in page.outputs):
            raise RuntimeError("published output escaped its OutputSet")
        if run_id is not None and any(
            output.binding.execution.run_id != run_id for output in page.outputs
        ):
            raise RuntimeError("published record provenance points to another run")
        if expected_payloads is not None:
            published_payloads = [
                {
                    key: value
                    for key, value in output.payload.items()
                    if key != "_provenance"
                }
                for output in page.outputs
            ]
            if sorted(map(canonical_json, published_payloads)) != sorted(
                map(canonical_json, expected_payloads)
            ):
                raise RuntimeError(
                    "independent published record read differs from the input oracle: "
                    f"published={published_payloads!r}; expected={expected_payloads!r}"
                )
        refs = {
            item.artifact.artifact_id: item.artifact
            for output in page.outputs
            for item in output.artifact_items
        }
        if not refs:
            raise RuntimeError("published output set contains no custody artifacts")
        artifact_digests: list[str] = []
        for ref in refs.values():
            body = artifact_request(tenant, ref.artifact_id)
            actual = hashlib.sha256(body).hexdigest()
            if len(body) != ref.size_bytes or actual != ref.digest.removeprefix(
                "sha256:"
            ):
                raise RuntimeError("BC06 artifact retrieval digest verification failed")
            artifact_digests.append("sha256:" + actual)
        return (
            sorted(refs),
            ",".join(sorted(artifact_digests)),
            sorted(output_ids),
            sorted(observation_ids),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=(
            "json",
            "html",
            "pagination",
            "pagination-planned",
            "redirect",
            "redirect-denied",
            "redirect-budget",
            "ack-loss",
            "pagination-cancel",
            "gateway-restart",
            "custody-interruption",
            "partial-stage-interruption",
            "oversized",
            "slow",
            "malformed",
            "invalid-output",
            "interrupted-body",
            "pagination-incomplete",
            "work-intake",
        ),
        default="json",
    )
    args = parser.parse_args()
    _configure_case(args.case)
    required = (
        "BC04_DATABASE_URL",
        "BC04_GRANT_SIGNING_KEY",
        "BC04_GRANT_KEY_ID",
        "ARTIFACT_CUSTODY_LOCAL_TOKEN",
        "ARTIFACT_CUSTODY_HTTP_URL",
    )
    if any(not os.environ.get(name) for name in required):
        raise SystemExit(
            "load the existing BC04 and BC06 local configuration before running this gate"
        )
    repo = PostgresDurableRunRepository(os.environ["BC04_DATABASE_URL"])
    authority_head = repo.authority_snapshot_head(AUTHORITY_ID)
    next_generation = (authority_head[0] + 1) if authority_head else 1
    run_id, request_id = (
        str(uuid.uuid4()),
        "controlled-live-request-" + uuid.uuid4().hex,
    )
    release_id = "controlled-live-release-" + uuid.uuid4().hex[:12]
    operation_token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    evidence_token = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    api_port, gateway_port = _port(), _port()
    project = "data-mozaic-authorized-live-" + CASE_KIND

    with tempfile.TemporaryDirectory(prefix="dm-authorized-live-json-") as temporary:
        root = Path(temporary)
        trusted_dir, capability_file, cap_seed, _initial_policy = configuration(
            root / "trusted", root / "tls"
        )
        policy = _policy()
        (trusted_dir / "policies.json").chmod(0o600)
        (trusted_dir / "policies.json").write_text(
            json.dumps({"policies": [policy]}, separators=(",", ":"))
        )
        (trusted_dir / "policies.json").chmod(0o444)
        (trusted_dir / "keys.json").chmod(0o600)
        (trusted_dir / "keys.json").write_text(
            json.dumps(
                {
                    "gateway-test-key": base64.urlsafe_b64encode(
                        Ed25519PrivateKey.from_private_bytes(cap_seed)
                        .public_key()
                        .public_bytes(
                            serialization.Encoding.Raw, serialization.PublicFormat.Raw
                        )
                    )
                    .decode()
                    .rstrip("=")
                }
            )
        )
        (trusted_dir / "keys.json").chmod(0o444)
        # Build the actual live runtime image, then verify and extract its compiled BotBuild.
        results = root / "results"
        results.mkdir(mode=0o777)
        results.chmod(0o777)
        env = os.environ | {
            "MOZAIC_LIVE_TRUSTED_CONFIG": str(trusted_dir),
            "MOZAIC_LIVE_CAPABILITY_FILE": str(capability_file),
            "MOZAIC_LIVE_RESULT_PARENT": str(results),
            "MOZAIC_GATEWAY_HOST_PORT": str(gateway_port),
            "MOZAIC_BC04_TEST_PORT": str(api_port),
            "MOZAIC_TEST_TLS_DIRECTORY": str(root / "tls"),
            "MOZAIC_TEST_SOURCE_UID": str(os.getuid()),
            "GATEWAY_TEST_STATIC_AUTHORIZATION": "0",
            "GATEWAY_BC04_AUTHORIZATION_URL": f"http://host.docker.internal:{api_port}/internal/v1/acquisition-operations/admit",
            "GATEWAY_BC04_AUTHORIZATION_TOKEN": operation_token,
            "GATEWAY_EVIDENCE_IMPORT_TOKEN": evidence_token,
            "MOZAIC_LIVE_PLAYBOOK": str(PLAYBOOK.relative_to(ROOT)),
            "MOZAIC_LIVE_FIXTURES": str(FIXTURES.relative_to(ROOT)),
        }
        if CASE_KIND in FAILURE_CASES:
            scenario_directory = ROOT / ".local/controlled-live-scenarios"
            scenario_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            scenario_id = uuid.uuid4().hex
            scenario_playbook = scenario_directory / f"{scenario_id}.yaml"
            scenario_fixtures = scenario_directory / scenario_id
            shutil.copytree(FIXTURES, scenario_fixtures)
            scenario_text = PLAYBOOK.read_text()
            if CASE_KIND == "pagination-incomplete":
                scenario_text = scenario_text.replace(
                    "completion_mode: bounded_window",
                    "completion_mode: natural_end",
                ).replace("max_pages: 2", "max_pages: 1")
                scenario_index = scenario_fixtures / "index.json"
                index_document = json.loads(scenario_index.read_bytes())
                scenario_index.write_text(
                    json.dumps(index_document, separators=(",", ":"))
                )
            else:
                scenario_text = scenario_text.replace("/v1/items", SOURCE_PATH)
                scenario_index = scenario_fixtures / "index.json"
                index_document = json.loads(scenario_index.read_bytes())
                for page in index_document["pages"]:
                    page["url"] = "https://api.fixture.test" + SOURCE_PATH
                scenario_index.write_text(
                    json.dumps(index_document, separators=(",", ":"))
                )
            scenario_playbook.write_text(scenario_text)
            scenario_playbook.chmod(0o600)
            atexit.register(
                lambda playbook=scenario_playbook, fixture_dir=scenario_fixtures: (
                    playbook.unlink(missing_ok=True),
                    shutil.rmtree(fixture_dir, ignore_errors=True),
                )
            )
            env["MOZAIC_LIVE_PLAYBOOK"] = str(scenario_playbook.relative_to(ROOT))
            env["MOZAIC_LIVE_FIXTURES"] = str(scenario_fixtures.relative_to(ROOT))
        compose = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(COMPOSE),
        ]
        _run(compose + ["--profile", "test", "build", "worker-scrapy-live"], env=env)
        image = f"{project}-worker-scrapy-live:latest"
        inspected = json.loads(
            _run(["docker", "image", "inspect", image], env=env, capture=True).stdout
        )[0]
        runtime_digest = inspected["Id"]
        container = _run(
            ["docker", "create", image], env=env, capture=True
        ).stdout.strip()
        build_dir = root / "live-bot"
        try:
            _run(
                ["docker", "cp", f"{container}:/opt/live-bot", str(build_dir)], env=env
            )
        finally:
            subprocess.run(
                ["docker", "rm", container], check=False, capture_output=True
            )
        manifest = json.loads((build_dir / "manifest.json").read_bytes())
        raw_manifest = (build_dir / "manifest.json").read_bytes()
        verify_bot_build(build_dir, hashlib.sha256(raw_manifest).hexdigest())
        build_digest = "sha256:" + hashlib.sha256(canonical_json(manifest)).hexdigest()
        if manifest.get("backend_id") != "scrapy-live-http" or not manifest.get(
            "build_id"
        ):
            raise RuntimeError("compiled BotBuild identity is invalid")

        registry = _contract_registry()
        input_contract = (
            registry.installed_reference("controlled.input.limit", "1.0.0")
            if CASE_KIND == "work-intake"
            else None
        )
        snapshot_bytes, trusted_keys = _authority_snapshot(
            policy,
            manifest,
            build_dir,
            ROOT / env["MOZAIC_LIVE_PLAYBOOK"],
            runtime_digest,
            release_id,
            next_generation,
            input_contract=ContractReference(
                input_contract.contract_id,
                input_contract.version,
                input_contract.digest,
            )
            if input_contract is not None
            else None,
        )
        authority = TrustedLiveAuthority(
            snapshot_bytes,
            trusted_public_keys=trusted_keys,
            minimum_generation=1,
            authority_registry=repo,
        )
        cap_signer = ExecutionCapabilitySigner(cap_seed, key_id="gateway-test-key")
        grant_seed, grant_key_id, _ = key_material()
        contract = _payload_contract(registry)
        with MongoResources(MongoSettings.from_env()) as resources:
            store = MongoOutputStore(resources)
            app = _application(store, registry)
            publisher = InProcessBC05Publisher(app, lose_ack=CASE_KIND == "ack-loss")
            coordinator = DurableExecutionCoordinator(
                repo,
                publisher,
                Ed25519PublicationGrantSigner(grant_seed, key_id=grant_key_id),
                lease_seconds=120,
                live_authority=authority,
                execution_capability_signer=cap_signer,
                execution_capability_verifier=cap_signer,
            )
            importer = TrustedAcquisitionApplication(
                repo,
                app,
                ArtifactCustodyHTTPClient(
                    os.environ["ARTIFACT_CUSTODY_HTTP_URL"],
                    os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"],
                ),
                finalization=coordinator,
            )
            if CASE_KIND == "work-intake":
                from acquisition_lifecycle_acceptance_support import active_lifecycle
                from work_intake.adapters.composition import compose_work_intake
                from work_intake.domain import LogicalInput

                lifecycle = active_lifecycle()
                input_reference = ContractReference(
                    input_contract.contract_id,
                    input_contract.version,
                    input_contract.digest,
                )
                operations = compose_work_intake(
                    database_url=os.environ["WORK_INTAKE_DATABASE_URL"],
                    migrations_root=ROOT / "migrations",
                    contract_root=ROOT / "contracts/payload/draft/v1",
                    lifecycle=lifecycle,
                    coordinator=coordinator,
                )
                pending_cancel = operations.submit(
                    logical_input=LogicalInput(
                        TENANT,
                        "input-P",
                        SOURCE_ID,
                        REVISION_ID,
                        input_reference,
                    ),
                    idempotency_scope="controlled-m5-acceptance",
                    idempotency_key="pending-cancellation-" + release_id,
                    release_id=release_id,
                    parameters={"limit": 1},
                )
                pending_cancel_result = operations.cancel(
                    TENANT, pending_cancel.intent.submission_id
                )
                if getattr(pending_cancel_result, "state", None).value != "cancelled":
                    raise RuntimeError("BC13 did not cancel pending work")
                ambiguous_cancel = operations.submit(
                    logical_input=LogicalInput(
                        TENANT,
                        "input-X",
                        SOURCE_ID,
                        REVISION_ID,
                        input_reference,
                    ),
                    idempotency_scope="controlled-m5-acceptance",
                    idempotency_key="ambiguous-cancellation-" + release_id,
                    release_id=release_id,
                    parameters={"limit": 1},
                )
                accepted_submissions = [ambiguous_cancel] + [
                    operations.submit(
                        logical_input=LogicalInput(
                            TENANT,
                            "input-" + label,
                            SOURCE_ID,
                            REVISION_ID,
                            input_reference,
                        ),
                        idempotency_scope="controlled-m5-acceptance",
                        idempotency_key="input-" + label + "-" + release_id,
                        release_id=release_id,
                        parameters={"limit": limit},
                    )
                    for label, limit in (("A", 1), ("B", 2), ("C", 3), ("D", 1))
                ]
                intentional_reacquisition = operations.submit(
                    logical_input=LogicalInput(
                        TENANT,
                        "input-A",
                        SOURCE_ID,
                        REVISION_ID,
                        input_reference,
                    ),
                    idempotency_scope="controlled-m5-acceptance",
                    idempotency_key="intentional-reacquisition-A-" + release_id,
                    release_id=release_id,
                    parameters={"limit": 1},
                )
                accepted_submissions.append(intentional_reacquisition)
                # Inject an acknowledgment loss after the real BC04 admission
                # commits. A fresh BC04 coordinator and BC13 composition must
                # resolve that receipt before attempting any new admission.
                admit_live_run = coordinator.admit_live_run
                lost_ack_run_ids: list[str] = []

                def admit_then_lose_ack(**admission):
                    admitted_snapshot = admit_live_run(**admission)
                    lost_ack_run_ids.append(admitted_snapshot.intent.run_id)
                    coordinator.admit_live_run = admit_live_run
                    raise RuntimeError("injected_bc04_admission_ack_loss")

                coordinator.admit_live_run = admit_then_lose_ack
                try:
                    operations.dispatch_pending(
                        worker_id="controlled-m5-dispatcher-before-ack-loss",
                        limit=1,
                        tenant_id=TENANT,
                    )
                except RuntimeError as error:
                    if str(error) != "injected_bc04_admission_ack_loss":
                        raise
                else:
                    raise RuntimeError("M5 acknowledgment-loss injection did not fire")
                coordinator = DurableExecutionCoordinator(
                    repo,
                    publisher,
                    Ed25519PublicationGrantSigner(grant_seed, key_id=grant_key_id),
                    lease_seconds=120,
                    live_authority=authority,
                    execution_capability_signer=cap_signer,
                    execution_capability_verifier=cap_signer,
                )
                importer.finalization = coordinator
                operations = compose_work_intake(
                    database_url=os.environ["WORK_INTAKE_DATABASE_URL"],
                    migrations_root=ROOT / "migrations",
                    contract_root=ROOT / "contracts/payload/draft/v1",
                    lifecycle=lifecycle,
                    coordinator=coordinator,
                )
                first_reconciliation = operations.reconcile(
                    worker_id="controlled-m5-reconciler-after-restart",
                    limit=1,
                    tenant_id=TENANT,
                )
                if (
                    len(first_reconciliation) != 1
                    or first_reconciliation[0].intent.submission_id
                    != ambiguous_cancel.intent.submission_id
                    or first_reconciliation[0].run_id != lost_ack_run_ids[0]
                ):
                    raise RuntimeError(
                        "fresh BC04 coordinator did not resolve the original admission receipt"
                    )
                ambiguous_cancellation_result = operations.cancel(
                    TENANT, ambiguous_cancel.intent.submission_id
                )
                ambiguous_cancellation_replay = operations.cancel(
                    TENANT, ambiguous_cancel.intent.submission_id
                )
                if (
                    ambiguous_cancellation_result.state.value != "cancelled"
                    or ambiguous_cancellation_replay.state.value != "cancelled"
                    or ambiguous_cancellation_result.intent.run_id
                    != lost_ack_run_ids[0]
                ):
                    raise RuntimeError(
                        "ambiguous-dispatch cancellation did not resolve and delegate the BC04 run"
                    )
                print(
                    "WORK_INTAKE_CANCELLATION_ACCEPTED "
                    + json.dumps(
                        {
                            "pending_submission_state": pending_cancel_result.state.value,
                            "pending_submission_was_not_dispatched": True,
                            "recovered_run_id": lost_ack_run_ids[0],
                            "bc04_terminal_state": ambiguous_cancellation_result.state.value,
                            "cancel_replay_same_outcome": (
                                ambiguous_cancellation_replay.state.value
                                == ambiguous_cancellation_result.state.value
                            ),
                        },
                        sort_keys=True,
                    )
                )
                dispatch_results = operations.reconcile(
                    worker_id="controlled-m5-reconciler-after-restart",
                    limit=10,
                    tenant_id=TENANT,
                )
                by_submission = {
                    item.intent.submission_id: item for item in dispatch_results
                }
                by_submission[ambiguous_cancel.intent.submission_id] = (
                    operations.inspect(TENANT, ambiguous_cancel.intent.submission_id)
                )
                accepted_submissions = [
                    by_submission[item.intent.submission_id]
                    for item in accepted_submissions
                ]
                if any(item.run_id is None for item in accepted_submissions):
                    raise RuntimeError("BC13 did not admit every controlled input")
                pending_after_dispatch = operations.inspect(
                    TENANT, pending_cancel.intent.submission_id
                )
                if (
                    pending_after_dispatch.state.value != "cancelled"
                    or pending_after_dispatch.run_id is not None
                ):
                    raise RuntimeError(
                        "pending cancellation was reclaimed by BC13 dispatch"
                    )
                if (
                    len(lost_ack_run_ids) != 1
                    or accepted_submissions[0].intent.submission_id
                    != ambiguous_cancel.intent.submission_id
                    or accepted_submissions[0].run_id != lost_ack_run_ids[0]
                ):
                    raise RuntimeError(
                        "BC13 admission recovery did not reuse the committed BC04 run: "
                        f"injected={lost_ack_run_ids!r}; "
                        f"reconciled={accepted_submissions[0].run_id!r}"
                    )
                print(
                    "WORK_INTAKE_ADMISSION_ACK_LOSS_RECOVERED "
                    + json.dumps(
                        {
                            "fresh_coordinator": True,
                            "fresh_composition": True,
                            "original_run_id": lost_ack_run_ids[0],
                            "recovered_run_id": accepted_submissions[0].run_id,
                            "deterministic_exception_injection": True,
                            "additional_capture_run": False,
                        },
                        sort_keys=True,
                    )
                )
                ambiguous_outcome = operations.inspect_execution(
                    TENANT, ambiguous_cancel.intent.submission_id
                )
                if ambiguous_outcome.state.value != "cancelled":
                    raise RuntimeError(
                        "BC13 cancellation inspection did not reflect BC04 terminal state"
                    )
            else:
                coordinator.admit_live_run(
                    tenant_id=TENANT,
                    run_id=run_id,
                    request_id=request_id,
                    idempotency_key="controlled-live-" + run_id,
                    deadline=datetime.now(UTC) + timedelta(minutes=3),
                    source_id=SOURCE_ID,
                    source_revision_id=REVISION_ID,
                    policy_id=POLICY_ID,
                    policy_version=POLICY_VERSION,
                    release_id=release_id,
                    build_id=str(manifest["build_id"]),
                    build_manifest_digest=build_digest,
                    runtime_image_digest=runtime_digest,
                    max_attempts=1,
                )
                accepted_submissions = [None]
            base_env = env
            m5_observations: list[dict[str, object]] = []
            m5_failures: list[dict[str, object]] = []
            for intake_submission in accepted_submissions:
                if intake_submission is not None:
                    if intake_submission.intent.input_id == "input-X":
                        continue
                    run_id = intake_submission.run_id
                    if run_id is None:
                        raise RuntimeError("BC13 submission has no CaptureRun")
                lease = coordinator.acquire_attempt(
                    TENANT, run_id, "protected-live-scrapy", "claim-" + run_id
                )
                env = base_env
                expected_records_path = EXPECTED
                if intake_submission is not None:
                    parameter_document = json.loads(
                        intake_submission.intent.parameters_json.decode("utf-8")
                    )
                    parameter_value = parameter_document["limit"]
                    parameter_file = root / (
                        "parameters-" + intake_submission.intent.submission_id + ".json"
                    )
                    parameter_file.write_bytes(intake_submission.intent.parameters_json)
                    # The protected worker runs as UID 65532. The temporary
                    # parent directory is private to this harness, while this
                    # mounted input file must be readable by that container UID.
                    parameter_file.chmod(0o444)
                    result_parent = results / intake_submission.intent.submission_id
                    result_parent.mkdir(mode=0o777)
                    result_parent.chmod(0o777)
                    expected_records_path = root / (
                        "expected-" + str(parameter_value) + ".jsonl"
                    )
                    expected_records_path.write_text(
                        json.dumps(
                            {
                                "enabled": True,
                                "id": f"parameter-{parameter_value}",
                                "quantity": parameter_value,
                                "unit_price": f"{parameter_value}.25",
                            },
                            separators=(",", ":"),
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    env = base_env | {
                        "MOZAIC_LIVE_RESULT_PARENT": str(result_parent),
                        "MOZAIC_LIVE_INPUT_PARAMETERS_FILE": str(parameter_file),
                        "MOZAIC_ENABLE_INPUT_PARAMETERS": "/run/mozaic/input-parameters",
                    }
                else:
                    parameter_value = None
                api_handler = acquisition_authorization_handler(
                    coordinator, bearer_token=operation_token
                )
                if CASE_KIND == "pagination-cancel":

                    class CancelAfterOperationAdmission(api_handler):
                        cancel_run_id = run_id

                        def do_POST(self):
                            super().do_POST()
                            coordinator.cancel(
                                TENANT,
                                self.cancel_run_id,
                                "cancel-after-admission-" + self.cancel_run_id,
                            )

                    api_handler = CancelAfterOperationAdmission
                restart_completed = threading.Event()
                restart_errors: list[str] = []
                if CASE_KIND == "gateway-restart":
                    admission_count = 0
                    admission_lock = threading.Lock()

                    def restart_gateway(
                        restart_env=env,
                        failure_kinds=restart_errors,
                        completed_event=restart_completed,
                    ):
                        try:
                            _run(
                                compose
                                + ["restart", "--timeout", "1", "acquisition-gateway"],
                                env=restart_env,
                                timeout=45,
                            )
                            _run(
                                compose + ["up", "--wait", "-d", "acquisition-gateway"],
                                env=restart_env,
                                timeout=45,
                            )
                        except (
                            RuntimeError,
                            subprocess.TimeoutExpired,
                            OSError,
                        ) as error:
                            failure_kinds.append(type(error).__name__)
                        finally:
                            completed_event.set()

                    class RestartGatewayAfterFirstAdmission(api_handler):
                        restart_lock = admission_lock

                        def do_POST(self):
                            nonlocal admission_count
                            super().do_POST()
                            with self.restart_lock:
                                admission_count += 1
                                should_restart = admission_count == 1
                            if should_restart:
                                threading.Thread(
                                    target=restart_gateway, daemon=True
                                ).start()

                    api_handler = RestartGatewayAfterFirstAdmission
                api_server = ThreadingHTTPServer(("0.0.0.0", api_port), api_handler)
                api_server.daemon_threads = True
                threading.Thread(target=api_server.serve_forever, daemon=True).start()
                try:
                    _run(
                        compose
                        + [
                            "up",
                            "--build",
                            "--wait",
                            "-d",
                            "acquisition-gateway",
                            "controlled-source",
                        ],
                        env=(
                            env
                            | {
                                "CONTROLLED_SOURCE_MALFORMED": (
                                    "1"
                                    if intake_submission is not None
                                    and intake_submission.intent.input_id == "input-D"
                                    else "0"
                                )
                            }
                        ),
                        timeout=300,
                    )
                    # Capability issuance follows gateway startup because gateway
                    # restart invalidates capabilities issued before its epoch.
                    capability = coordinator.issue_execution_capability(
                        lease, audience=AUDIENCE
                    )
                    encoded = (
                        base64.urlsafe_b64encode(
                            json.dumps(capability, separators=(",", ":")).encode()
                        )
                        .decode()
                        .rstrip("=")
                    )
                    capability_file.chmod(0o600)
                    write(capability_file, encoded.encode(), 0o444)
                    try:
                        _run(
                            compose
                            + [
                                "--profile",
                                "test",
                                "run",
                                "--rm",
                                "worker-scrapy-live",
                            ],
                            env=env,
                            timeout=120,
                            capture=CASE_KIND
                            in {
                                "redirect-denied",
                                "redirect-budget",
                                "pagination-cancel",
                                "gateway-restart",
                                *FAILURE_CASES,
                            }
                            or (
                                intake_submission is not None
                                and intake_submission.intent.input_id == "input-D"
                            ),
                        )
                    except RuntimeError as error:
                        expected_m5_failure = (
                            intake_submission is not None
                            and intake_submission.intent.input_id == "input-D"
                        )
                        if CASE_KIND in FAILURE_CASES:
                            diagnostic = _worker_failure_diagnostic(error)
                            expected_diagnostics = FAILURE_DIAGNOSTICS[CASE_KIND]
                            if diagnostic is None or not any(
                                marker.casefold() in diagnostic.casefold()
                                for marker in expected_diagnostics
                            ):
                                raise RuntimeError(
                                    "controlled live scenario failed without its "
                                    f"expected worker diagnostic: {CASE_KIND}; "
                                    f"observed={diagnostic!r}"
                                ) from error
                        elif expected_m5_failure:
                            diagnostic = _worker_failure_diagnostic(error)
                            if diagnostic is None or not any(
                                marker.casefold() in diagnostic.casefold()
                                for marker in FAILURE_DIAGNOSTICS["malformed"]
                            ):
                                raise RuntimeError(
                                    "parameterized M5 failure did not observe the controlled malformed response"
                                ) from error
                        expected_rejection = (
                            (
                                CASE_KIND in {"redirect-denied", "redirect-budget"}
                                and "redirect_denied" in str(error)
                            )
                            or (
                                CASE_KIND == "pagination-cancel"
                                and "current_attempt" in str(error)
                            )
                            or CASE_KIND in FAILURE_CASES
                            or expected_m5_failure
                            or CASE_KIND == "gateway-restart"
                        )
                        if not expected_rejection:
                            raise
                        if CASE_KIND == "gateway-restart":
                            if not restart_completed.wait(45) or restart_errors:
                                raise RuntimeError(
                                    "gateway restart did not complete successfully"
                                ) from error
                            stale_results = root / "stale-results"
                            stale_results.mkdir(mode=0o777)
                            stale_results.chmod(0o777)
                            stale_env = env | {
                                "MOZAIC_LIVE_RESULT_PARENT": str(stale_results)
                            }
                            try:
                                _run(
                                    compose
                                    + [
                                        "--profile",
                                        "test",
                                        "run",
                                        "--rm",
                                        "worker-scrapy-live",
                                    ],
                                    env=stale_env,
                                    timeout=60,
                                    capture=True,
                                )
                            except RuntimeError as stale_error:
                                if "capability_expired_or_stale" not in str(
                                    stale_error
                                ):
                                    raise RuntimeError(
                                        "gateway restart did not invalidate the prior capability"
                                    ) from stale_error
                            else:
                                raise RuntimeError(
                                    "gateway restart allowed the prior capability"
                                )
                        if CASE_KIND == "pagination-cancel":
                            failed = coordinator.inspect(TENANT, run_id)
                            expected_state = "cancelled"
                        else:
                            failed = coordinator.fail_attempt(
                                lease,
                                FailureDisposition.PERMANENT,
                                (
                                    "controlled_redirect_denied"
                                    if CASE_KIND
                                    in {"redirect-denied", "redirect-budget"}
                                    else "controlled_response_failure"
                                ),
                                "fail-" + run_id,
                            )
                            expected_state = "failed"
                        if (
                            failed.state.value != expected_state
                            or failed.publication_receipt is not None
                        ):
                            raise RuntimeError(
                                "denied redirect did not fail BC04 without publication"
                            ) from error
                        if expected_m5_failure:
                            failed_submission = operations.inspect(
                                TENANT, intake_submission.intent.submission_id
                            )
                            if failed_submission.run_id != run_id:
                                raise RuntimeError(
                                    "failed M5 submission lost its original run correlation"
                                )
                            authoritative = operations.inspect_execution(
                                TENANT, intake_submission.intent.submission_id
                            )
                            if authoritative.state.value != "failed":
                                raise RuntimeError(
                                    "BC13 inspection did not reflect authoritative BC04 failure"
                                )
                            m5_failures.append(
                                {
                                    "input_id": failed_submission.intent.input_id,
                                    "submission_id": failed_submission.intent.submission_id,
                                    "run_id": run_id,
                                    "bc04_state": authoritative.state.value,
                                    "publication_receipt": failed.publication_receipt,
                                    "failure_diagnostic": diagnostic,
                                }
                            )
                            print(
                                "WORK_INTAKE_ACQUISITION_FAILURE_NO_PUBLICATION "
                                + json.dumps(m5_failures[-1], sort_keys=True)
                            )
                            continue
                        print(
                            (
                                "AUTHORIZED_CONTROLLED_LIVE_FAILURE_ACCEPTED "
                                if CASE_KIND in FAILURE_CASES
                                else "AUTHORIZED_CONTROLLED_LIVE_NEGATIVE_ACCEPTED "
                            )
                            + json.dumps(
                                {
                                    "run_id": run_id,
                                    "attempt_id": lease.attempt_id,
                                    "state": failed.state.value,
                                    "publication_receipt": failed.publication_receipt,
                                    "diagnostic": (
                                        diagnostic
                                        if CASE_KIND in FAILURE_CASES
                                        else "current_attempt_revoked"
                                        if CASE_KIND == "pagination-cancel"
                                        else CASE_KIND.replace("-", "_")
                                    ),
                                },
                                sort_keys=True,
                            )
                        )
                        return 0
                    else:
                        if CASE_KIND in {"redirect-denied", "redirect-budget"}:
                            raise RuntimeError("gateway unexpectedly allowed redirect")
                    result_dir = Path(env["MOZAIC_LIVE_RESULT_PARENT"]) / "result"

                    def verify_result(
                        directory: Path, tenant: str = TENANT, run: str = run_id
                    ):
                        return importer.verify_live_result(
                            tenant,
                            run,
                            result_dir=directory,
                            gateway_evidence_url=f"http://127.0.0.1:{gateway_port}",
                            gateway_evidence_token=evidence_token,
                            build_root=build_dir,
                            max_response_bytes=int(policy["max_response_bytes"]),
                        )

                    for test_name, overrides in (
                        ("stale-run", {"run": str(uuid.uuid4())}),
                        ("cross-tenant", {"tenant": "tenant-not-authorized"}),
                    ):
                        try:
                            verify_result(
                                result_dir,
                                tenant=overrides.get("tenant", TENANT),
                                run=overrides.get("run", run_id),
                            )
                        except FixtureImportError:
                            pass
                        else:
                            raise RuntimeError(
                                f"live evidence importer accepted {test_name}"
                            )
                    unknown_bundle = root / ("unknown-fetch-result-" + run_id)
                    shutil.copytree(result_dir, unknown_bundle)
                    unknown_rows = [
                        json.loads(line)
                        for line in (unknown_bundle / "fetch-evidence.jsonl")
                        .read_text()
                        .splitlines()
                    ]
                    unknown_rows[0]["fetch_id"] = "0" * 32
                    (unknown_bundle / "fetch-evidence.jsonl").write_text(
                        "".join(
                            json.dumps(row, separators=(",", ":")) + "\n"
                            for row in unknown_rows
                        )
                    )
                    try:
                        verify_result(unknown_bundle)
                    except FixtureImportError:
                        pass
                    else:
                        raise RuntimeError("live importer accepted an unknown fetch id")
                    corrupt_bundle = root / ("corrupt-result-" + run_id)
                    shutil.copytree(result_dir, corrupt_bundle)
                    evidence_rows = [
                        json.loads(line)
                        for line in (corrupt_bundle / "fetch-evidence.jsonl")
                        .read_text()
                        .splitlines()
                    ]
                    evidence_rows[0]["body_sha256"] = "0" * 64
                    (corrupt_bundle / "fetch-evidence.jsonl").write_text(
                        json.dumps(evidence_rows[0], separators=(",", ":")) + "\n"
                    )
                    try:
                        verify_result(corrupt_bundle)
                    except FixtureImportError:
                        pass
                    else:
                        raise RuntimeError(
                            "live importer accepted divergent worker evidence"
                        )
                    verified = verify_result(result_dir)
                    expected_records = [
                        json.loads(line)
                        for line in expected_records_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                        if line
                    ]
                    actual_records = [
                        {
                            key: value
                            for key, value in record.items()
                            if key != "_provenance"
                        }
                        for record in verified.records
                    ]
                    if actual_records != expected_records:
                        raise RuntimeError(
                            "live acquisition differs from independent test oracle"
                        )
                    if intake_submission is not None:
                        expected_target = (
                            "https://api.fixture.test/v1/items?limit="
                            + str(parameter_value)
                        )
                        if (
                            len(verified.pages) != 1
                            or verified.pages[0].url != expected_target
                        ):
                            raise RuntimeError(
                                "gateway FetchEvidence target differs from the BC13 parameter binding"
                            )
                        if any(
                            record.get("_provenance", {}).get("source_url")
                            != expected_target
                            for record in verified.records
                        ):
                            raise RuntimeError(
                                "record provenance differs from the authorized input target"
                            )
                    if CASE_KIND in {"pagination", "pagination-planned"} and tuple(
                        urlsplit(page.url).path for page in verified.pages
                    ) != tuple(SOURCE_PATHS):
                        raise RuntimeError(
                            "controlled live page sequence differs from its oracle"
                        )
                    producer = ProducerIdentity(
                        str(manifest["bot_id"]),
                        "collector-live-v1",
                        "collector.scrapy-live-http",
                        str(manifest["backend_version"]),
                        release_id,
                        build_digest,
                    )
                    import_arguments = {
                        "bundle": verified,
                        "producer": producer,
                        "payload_contract": contract,
                        "artifact_representation": "decoded_http_entity",
                        "retention_policy_ref": str(
                            policy["evidence_retention_policy_ref"]
                        ),
                        "artifact_title": "Controlled live "
                        + CASE_KIND.upper()
                        + " HTTP entity",
                        "artifact_role": "acquired-source-response",
                    }
                    importer_app = (
                        _ImportInterruptionProxy(
                            app,
                            "custody-before-stage"
                            if CASE_KIND == "custody-interruption"
                            else "partial-stage",
                        )
                        if CASE_KIND
                        in {"custody-interruption", "partial-stage-interruption"}
                        else app
                    )
                    try:
                        output_set_id, manifest_digest = TrustedAcquisitionApplication(
                            repo,
                            importer_app,
                            ArtifactCustodyHTTPClient(
                                os.environ["ARTIFACT_CUSTODY_HTTP_URL"],
                                os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"],
                            ),
                        ).import_verified_result(TENANT, run_id, **import_arguments)
                    except _InjectedImportInterruption:
                        if CASE_KIND not in {
                            "custody-interruption",
                            "partial-stage-interruption",
                        }:
                            raise
                        custodied: list[str] = []
                        for page in verified.pages:
                            artifact_id = stable_identifier(
                                "artifact-",
                                {
                                    "tenant": TENANT,
                                    "run": run_id,
                                    "attempt": lease.attempt_id,
                                    "url": page.url,
                                    "page": page.evidence_ref,
                                    "digest": page.digest,
                                },
                            )
                            stored = artifact_request(TENANT, artifact_id)
                            if hashlib.sha256(
                                stored
                            ).hexdigest() != page.digest.removeprefix("sha256:"):
                                raise RuntimeError(
                                    "BC06 recovery artifact bytes diverged"
                                )
                            custodied.append(artifact_id)
                        visible_outputs = ()
                        incomplete_set = (
                            importer_app.output_set_id
                            if CASE_KIND == "partial-stage-interruption"
                            else None
                        )
                        if incomplete_set is not None:
                            visible_outputs = app.read_published_outputs(
                                TENANT, incomplete_set, limit=10
                            ).outputs
                        failed = coordinator.fail_attempt(
                            lease,
                            FailureDisposition.PERMANENT,
                            "controlled_import_interrupted",
                            "fail-import-" + run_id,
                        )
                        if (
                            failed.state.value != "failed"
                            or failed.publication_receipt is not None
                            or visible_outputs
                        ):
                            raise RuntimeError(
                                "interrupted live import exposed output or terminal success"
                            )
                        print(
                            "AUTHORIZED_CONTROLLED_LIVE_IMPORT_INTERRUPTION "
                            + json.dumps(
                                {
                                    "run_id": run_id,
                                    "attempt_id": lease.attempt_id,
                                    "state": failed.state.value,
                                    "custodied_artifact_ids": custodied,
                                    "output_set_id": incomplete_set,
                                    "staged_outputs": importer_app.staged,
                                    "published_outputs_visible": len(visible_outputs),
                                    "publication_receipt": failed.publication_receipt,
                                },
                                sort_keys=True,
                            )
                        )
                        return 0
                    replay_set_id, replay_manifest_digest = (
                        importer.import_verified_result(
                            TENANT, run_id, **import_arguments
                        )
                    )
                    if (replay_set_id, replay_manifest_digest) != (
                        output_set_id,
                        manifest_digest,
                    ):
                        raise RuntimeError(
                            "live result replay changed OutputSet identity or manifest"
                        )
                    before_replay = app.inspect_output_set(TENANT, output_set_id)
                    after_replay = app.inspect_output_set(TENANT, replay_set_id)
                    if before_replay is None or after_replay is None:
                        raise RuntimeError("live replay OutputSet disappeared")
                    durable_membership_before = (
                        before_replay.state.value,
                        before_replay.member_count,
                        before_replay.encoded_output_bytes,
                        before_replay.artifact_reference_count,
                        before_replay.custody_reference_count,
                        before_replay.manifest_digest,
                    )
                    durable_membership_after = (
                        after_replay.state.value,
                        after_replay.member_count,
                        after_replay.encoded_output_bytes,
                        after_replay.artifact_reference_count,
                        after_replay.custody_reference_count,
                        after_replay.manifest_digest,
                    )
                    if (
                        durable_membership_before != durable_membership_after
                        or after_replay.state.value != "sealed"
                        or after_replay.member_count != len(verified.records)
                        or after_replay.artifact_reference_count
                        != len(verified.records)
                        or after_replay.custody_reference_count != len(verified.records)
                        or after_replay.manifest_digest != manifest_digest
                    ):
                        raise RuntimeError(
                            "live replay changed durable OutputSet membership or custody references"
                        )
                    try:
                        reservation, published_receipt = importer.finalize_output_set(
                            lease, output_set_id, manifest_digest
                        )
                    except TimeoutError:
                        if CASE_KIND != "ack-loss":
                            raise
                        pending = coordinator.inspect(TENANT, run_id)
                        if (
                            pending.reservation is None
                            or pending.state.value != "finalization_pending"
                        ):
                            raise RuntimeError(
                                "lost acknowledgment did not preserve BC04 reservation"
                            )
                        bc05_receipt = publisher.get_publication_receipt(
                            TENANT, output_set_id
                        )
                        if bc05_receipt is None:
                            raise RuntimeError(
                                "BC05 did not commit before simulated acknowledgment loss"
                            )
                        reservation = pending.reservation
                        recovery_coordinator = DurableExecutionCoordinator(
                            repo,
                            publisher,
                            Ed25519PublicationGrantSigner(
                                grant_seed, key_id=grant_key_id
                            ),
                            lease_seconds=120,
                            live_authority=authority,
                            execution_capability_signer=cap_signer,
                            execution_capability_verifier=cap_signer,
                        )
                        recovery_importer = TrustedAcquisitionApplication(
                            repo,
                            app,
                            ArtifactCustodyHTTPClient(
                                os.environ["ARTIFACT_CUSTODY_HTTP_URL"],
                                os.environ["ARTIFACT_CUSTODY_LOCAL_TOKEN"],
                            ),
                            finalization=recovery_coordinator,
                        )
                        final = recovery_importer.reconcile_finalization(TENANT, run_id)
                        published_receipt = final.publication_receipt
                        if published_receipt != bc05_receipt:
                            raise RuntimeError(
                                "BC04 reconciliation receipt differs from durable BC05 receipt"
                            )
                    final = coordinator.inspect(TENANT, run_id)
                    if (
                        final.state.value != "succeeded"
                        or final.publication_receipt is None
                    ):
                        raise RuntimeError(
                            "BC04 did not record successful authorized publication"
                        )
                    if final.publication_receipt != published_receipt:
                        raise RuntimeError(
                            "BC04 durable receipt differs from BC05 publication response"
                        )
                    durable_bc05_receipt = publisher.get_publication_receipt(
                        TENANT, output_set_id
                    )
                    if durable_bc05_receipt != final.publication_receipt:
                        raise RuntimeError(
                            "independent BC05 receipt differs from BC04 terminal receipt"
                        )
                    (
                        artifacts,
                        artifact_digests,
                        output_ids,
                        observation_ids,
                    ) = _verify_outputs(
                        TENANT,
                        run_id,
                        output_set_id,
                        len(verified.records),
                        contract,
                        expected_payloads=expected_records,
                    )
                    evidence_ids = [page.evidence_ref for page in verified.pages]
                    correlation: dict[str, object] = {}
                    if intake_submission is not None:
                        stored_submission = operations.inspect(
                            TENANT, intake_submission.intent.submission_id
                        )
                        authoritative_outcome = operations.inspect_execution(
                            TENANT, intake_submission.intent.submission_id
                        )
                        if (
                            stored_submission.run_id != run_id
                            or stored_submission.intent.input_id
                            != intake_submission.intent.input_id
                            or authoritative_outcome.state.value != "succeeded"
                            or authoritative_outcome.intent.run_id != run_id
                        ):
                            raise RuntimeError(
                                "BC13 submission does not resolve its authoritative BC04 outcome"
                            )
                        correlation = {
                            "input_id": stored_submission.intent.input_id,
                            "submission_id": stored_submission.intent.submission_id,
                            "submission_parameter_digest": stored_submission.intent.parameters_digest,
                            "authorized_request_target": verified.pages[0].url,
                        }
                        m5_observations.append(
                            {
                                **correlation,
                                "run_id": run_id,
                                "output_set_id": output_set_id,
                                "publication_receipt": final.publication_receipt,
                                "artifact_ids": artifacts,
                                "artifact_digests": artifact_digests,
                                "output_ids": output_ids,
                                "observation_ids": observation_ids,
                                "record_content": actual_records,
                            }
                        )
                    print(
                        "AUTHORIZED_CONTROLLED_LIVE_"
                        + CASE_KIND.upper()
                        + "_ACCEPTED "
                        + json.dumps(
                            {
                                "run_id": run_id,
                                "attempt_id": lease.attempt_id,
                                "generation": lease.generation,
                                "build_id": manifest["build_id"],
                                "build_manifest_digest": build_digest,
                                "runtime_image_digest": runtime_digest,
                                "release_id": release_id,
                                **correlation,
                                "payload_contract": {
                                    "contract_id": contract.contract_id,
                                    "version": contract.version,
                                    "digest": contract.digest,
                                },
                                "fetch_ids": evidence_ids,
                                "redirect_counts": [
                                    page.redirect_count for page in verified.pages
                                ],
                                "artifact_ids": artifacts,
                                "artifact_digests": artifact_digests,
                                "output_set_id": output_set_id,
                                "manifest_digest": manifest_digest,
                                "reservation_id": reservation.reservation_id,
                                "publication_receipt": final.publication_receipt,
                                "records": len(verified.records),
                                "state": final.state.value,
                            },
                            sort_keys=True,
                        )
                    )
                finally:
                    api_server.shutdown()
                    api_server.server_close()
                    _run(compose + ["down", "--remove-orphans"], env=env, timeout=90)
            if CASE_KIND == "work-intake":
                revoked_pending = operations.submit(
                    logical_input=LogicalInput(
                        TENANT,
                        "input-revocation-check",
                        SOURCE_ID,
                        REVISION_ID,
                        input_reference,
                    ),
                    idempotency_scope="controlled-m5-acceptance",
                    idempotency_key="revocation-check-" + release_id,
                    release_id=release_id,
                    parameters={"limit": 1},
                )
                lifecycle.revoke_release(
                    release_id, actor_id="controlled-test-operator"
                )
                lifecycle.publish_authority_snapshot()
                revoked_result = operations.dispatch_pending(
                    worker_id="controlled-m5-after-revocation",
                    limit=1,
                    tenant_id=TENANT,
                )
                revoked_submission = operations.inspect(
                    TENANT, revoked_pending.intent.submission_id
                )
                if (
                    len(revoked_result) != 1
                    or revoked_submission.state.value != "rejected"
                    or revoked_submission.run_id is not None
                    or revoked_submission.diagnostic_code != "release_revoked"
                    or repo.get_by_idempotency(
                        TENANT, revoked_pending.intent.dispatch_operation_id
                    )
                    is not None
                ):
                    raise RuntimeError(
                        "effective release revocation did not reject new BC13 admission"
                    )
                input_a = next(
                    item for item in m5_observations if item["input_id"] == "input-A"
                )
                (
                    artifacts_after_revocation,
                    digests_after_revocation,
                    _output_ids_after_revocation,
                    _observation_ids_after_revocation,
                ) = _verify_outputs(
                    TENANT,
                    str(input_a["run_id"]),
                    str(input_a["output_set_id"]),
                    1,
                    contract,
                    expected_payloads=[
                        {
                            "id": "parameter-1",
                            "quantity": 1,
                            "unit_price": "1.25",
                            "enabled": True,
                        }
                    ],
                )
                if not artifacts_after_revocation or not digests_after_revocation:
                    raise RuntimeError(
                        "previously published output became inaccessible after release revocation"
                    )
                print(
                    "WORK_INTAKE_REVOCATION_ACCEPTED "
                    + json.dumps(
                        {
                            "new_submission_state": revoked_submission.state.value,
                            "new_run_created": False,
                            "authority_generation": repo.authority_snapshot_head(
                                AUTHORITY_ID
                            )[0],
                            "previous_publication_still_readable": True,
                        },
                        sort_keys=True,
                    )
                )
                if (
                    len(m5_observations) != 4
                    or len(m5_failures) != 1
                    or len({item["input_id"] for item in m5_observations}) != 3
                    or len({item["submission_id"] for item in m5_observations}) != 4
                    or len(
                        {
                            item["submission_parameter_digest"]
                            for item in m5_observations
                        }
                    )
                    != 3
                    or len({item["run_id"] for item in m5_observations}) != 4
                    or len({item["output_set_id"] for item in m5_observations}) != 4
                    or len(
                        {
                            output_id
                            for item in m5_observations
                            for output_id in item["output_ids"]
                        }
                    )
                    != 4
                    or len(
                        {
                            observation_id
                            for item in m5_observations
                            for observation_id in item["observation_ids"]
                        }
                    )
                    != 4
                ):
                    raise RuntimeError(
                        "M5 acceptance did not preserve three distinct Input-to-output identities"
                    )
                input_a_observations = [
                    item for item in m5_observations if item["input_id"] == "input-A"
                ]
                if (
                    len(input_a_observations) != 2
                    or input_a_observations[0]["submission_id"]
                    == input_a_observations[1]["submission_id"]
                    or input_a_observations[0]["run_id"]
                    == input_a_observations[1]["run_id"]
                    or input_a_observations[0]["output_set_id"]
                    == input_a_observations[1]["output_set_id"]
                ):
                    raise RuntimeError(
                        "intentional reacquisition did not create distinct submission, run and OutputSet identities"
                    )
                print(
                    "WORK_INTAKE_INTENTIONAL_REACQUISITION "
                    + json.dumps(input_a_observations, sort_keys=True)
                )
                print(
                    "WORK_INTAKE_THREE_INPUTS_CORRELATED "
                    + json.dumps(m5_observations, sort_keys=True)
                )

    if os.environ.get("MOZAIC_LIFECYCLE_ACCEPTANCE") == "1":
        from acquisition_lifecycle_acceptance_support import (
            revoke_and_verify_admission_denial,
        )

        print(
            "M4_RELEASE_REVOCATION_ACCEPTED "
            + json.dumps(revoke_and_verify_admission_denial(), sort_keys=True)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
