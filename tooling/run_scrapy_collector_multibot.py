"""Build and exercise an assembled multi-BotBuild Scrapy image."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import yaml
from collector_engineering.backend_scrapy_fixture import (
    compile_build as compile_scrapy_build,
)
from collector_engineering.playbook_v2 import (
    build_typed_ir,
    load_playbook_v2,
)
from collector_engineering.syntax import PlaybookError
from jsonschema import Draft202012Validator
from mozaic_bot_build import canonical_json
from scrapy_collector.catalog.assembly import assemble_catalog
from scrapy_collector.catalog.model import load_catalog, runtime_platform_id
from scrapy_collector.execution.context import ExecutionContext
from scrapy_collector.execution.launcher import run

ROOT = Path(__file__).resolve().parents[1]
WORKER_DOCKERFILE = ROOT / "apps/workers/scrapy-collector/Dockerfile"


def _compile_fixture_bot(
    temp: Path,
    example_id: str,
    *,
    revision: str | None = None,
    inject_callback_failure: bool = False,
    target_platform: str,
) -> tuple[dict, Path, Path]:
    example = ROOT / "examples/playbooks" / example_id
    source_playbook = example / "capture-playbook.yaml"
    fixture_input = temp / "fixture-input" / f"{example_id}-{revision or 'default'}"
    shutil.copytree(example / "fixtures", fixture_input)
    playbook_path = source_playbook
    original_digest = hashlib.sha256(source_playbook.read_bytes()).hexdigest()
    if revision is not None:
        text = source_playbook.read_text(encoding="utf-8")
        text = text.replace("revision: fixture-1", f"revision: {revision}", 1)
        playbook_path = temp / "playbooks" / f"{example_id}-{revision}.yaml"
        playbook_path.parent.mkdir(parents=True, exist_ok=True)
        playbook_path.write_text(text, encoding="utf-8")
        index_path = fixture_input / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["source"]["revision"] = revision
        index_path.write_bytes(canonical_json(index))
        original_digest = hashlib.sha256(playbook_path.read_bytes()).hexdigest()
    parsed = load_playbook_v2(playbook_path)
    ir = build_typed_ir(parsed, fixture_root=fixture_input)
    staged_build = temp / "staged-builds" / f"{example_id}-{revision or 'default'}"
    staged_build.parent.mkdir(parents=True, exist_ok=True)
    compile_arguments = {
        "fixture_root": fixture_input,
        "output": staged_build,
        "lockfile": ROOT / "uv.lock",
        "playbook_sha256": original_digest,
        "target_platform": target_platform,
    }
    if inject_callback_failure:
        # Inject into generated callback code before compile_build calculates
        # generated-file hashes, build_id, and the runtime manifest. The normal
        # verifier therefore still authenticates this synthetic test BotBuild.
        from collector_engineering import backend_scrapy_fixture

        original_generate_spider = backend_scrapy_fixture.generate_spider
        item_yield = b"            yield {'_sort_key': [document['page_index'], selected['record_index']], 'record': record}\n"

        def generate_failing_spider(*args, **kwargs):
            generated = original_generate_spider(*args, **kwargs)
            if generated.count(item_yield) != 1:
                raise RuntimeError(
                    "[TEST_CALLBACK_INJECTION_FAILED] expected one generated item yield"
                )
            return generated.replace(
                item_yield,
                item_yield
                + b"        if not self.records:\n            raise RuntimeError('[TEST_CALLBACK_ORDER] record pipeline did not run before injected failure')\n        raise RuntimeError('[TEST_CALLBACK_EXCEPTION] intentional E2E failure after record')\n",
                1,
            )

        with patch.object(
            backend_scrapy_fixture, "generate_spider", generate_failing_spider
        ):
            manifest = compile_scrapy_build(ir, **compile_arguments)
    else:
        manifest = compile_scrapy_build(ir, **compile_arguments)
    schema = json.loads(
        (ROOT / "contracts/bot-build/draft/v2/BotBuildManifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(manifest)
    return manifest, staged_build, fixture_input


def _prepare_catalog(
    temp: Path, target_platform: str
) -> tuple[Path, list[dict], dict[str, Path]]:
    temp.mkdir(parents=True, exist_ok=True)
    build_root = temp / "builds"
    fixture_root = temp / "fixture-inputs-by-build"
    build_root.mkdir()
    fixture_root.mkdir()
    builds = [
        _compile_fixture_bot(temp, "html-simple", target_platform=target_platform),
        _compile_fixture_bot(
            temp, "html-simple", revision="fixture-2", target_platform=target_platform
        ),
        _compile_fixture_bot(
            temp,
            "html-simple",
            revision="callback-failure-test",
            inject_callback_failure=True,
            target_platform=target_platform,
        ),
        _compile_fixture_bot(temp, "json-api", target_platform=target_platform),
        _compile_fixture_bot(
            temp, "multipage-catalog", target_platform=target_platform
        ),
    ]
    entries = []
    fixtures_by_example: dict[str, Path] = {}
    for manifest, staged_build, source_fixtures in builds:
        build_id = manifest["build_id"]
        installed_build = build_root / build_id
        shutil.copytree(staged_build, installed_build)
        fixture_dest = fixture_root / build_id
        shutil.copytree(source_fixtures, fixture_dest)
        manifest_bytes = (installed_build / "manifest.json").read_bytes()
        entries.append(
            {
                "bot_id": manifest["bot_id"],
                "build_id": build_id,
                "bot_build_manifest_digest": hashlib.sha256(manifest_bytes).hexdigest(),
                "state": "include",
            }
        )
        fixtures_by_example[build_id] = source_fixtures
    source_manifest = temp / "bot-catalog.yaml"
    source_manifest.write_text(
        yaml.safe_dump(
            {"catalog_version": "bot-catalog-source/v1", "entries": entries},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assembled = temp / "assembled"
    assemble_catalog(
        manifest_path=source_manifest,
        build_root=build_root,
        fixture_root=fixture_root,
        output=assembled,
        lockfile=ROOT / "uv.lock",
        target_platform=target_platform,
    )
    return assembled, entries, fixtures_by_example


def _assert_browser_capability_rejected(temp: Path) -> None:
    example = ROOT / "examples/playbooks/browser-capability-unsupported"
    playbook_path = example / "capture-playbook.yaml"
    parsed = load_playbook_v2(playbook_path)
    ir = build_typed_ir(parsed, fixture_root=example / "fixtures")
    try:
        compile_scrapy_build(
            ir,
            fixture_root=example / "fixtures",
            output=temp / "unsupported-browser-build",
            lockfile=ROOT / "uv.lock",
            playbook_sha256=hashlib.sha256(playbook_path.read_bytes()).hexdigest(),
            target_platform=runtime_platform_id(),
        )
    except PlaybookError:
        return
    raise RuntimeError(
        "[MULTIBOT_CAPABILITY_ACCEPTED] browser capability must be rejected"
    )


def _make_tree_removable(root: Path) -> None:
    for directory, dirnames, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for dirname in dirnames:
            (current / dirname).chmod(0o700)
        current.chmod(0o700)


def _check_records(
    output_dir: Path, expected_path: Path, *, source_revision: str | None = None
) -> None:
    actual = output_dir / "records.jsonl"
    expected = expected_path.read_bytes()
    if source_revision is not None:
        expected = expected.replace(
            b'"source_revision":"fixture-1"',
            f'"source_revision":"{source_revision}"'.encode(),
        )
    if not actual.is_file() or actual.read_bytes() != expected:
        actual_text = (
            actual.read_text(encoding="utf-8") if actual.exists() else "<missing>"
        )
        raise RuntimeError(
            f"[MULTIBOT_RECORDS_MISMATCH] output differs for {output_dir}: {actual_text}"
        )
    report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    if (
        report.get("state") != "passed"
        or report.get("records_sha256")
        != hashlib.sha256(actual.read_bytes()).hexdigest()
    ):
        raise RuntimeError(
            f"[MULTIBOT_REPORT_INVALID] output report differs for {output_dir}"
        )


def _local_execution_acceptance(
    assembled: Path, entries: list[dict], temp: Path
) -> None:
    catalog = load_catalog(assembled)
    output_root = temp / "local-output"
    output_root.mkdir()
    html_entries = []
    for entry in entries:
        bot = catalog.resolve(entry["bot_id"], entry["build_id"])
        result = run(catalog, bot.bot_id, bot.build_id, ExecutionContext(output_root))
        build_manifest = json.loads(
            (assembled / "builds" / bot.build_id / "manifest.json").read_text()
        )
        if build_manifest["source"]["source_revision"] == "callback-failure-test":
            if (
                result["state"] != "failed"
                or "[TEST_CALLBACK_EXCEPTION] intentional E2E failure after record"
                not in result.get("diagnostic", "")
                or (output_root / result["execution_id"]).exists()
                or (output_root / f".{result['execution_id']}.work").exists()
            ):
                raise RuntimeError(f"[MULTIBOT_CALLBACK_FAILURE_INVALID] {result}")
            # Exercise the public CLI boundary too: its process exit code and
            # terminal JSON must agree with the real Scrapy callback failure.
            cli_environment = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "SCRAPY_COLLECTOR_CATALOG_ROOT": str(assembled),
                "SCRAPY_COLLECTOR_OUTPUT_ROOT": str(output_root),
            }
            cli = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "scrapy_collector",
                    "run",
                    "--bot-id",
                    bot.bot_id,
                    "--build-id",
                    bot.build_id,
                ],
                cwd=ROOT,
                env=cli_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            try:
                cli_result = json.loads(cli.stdout)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    "[MULTIBOT_CALLBACK_CLI_INVALID] launcher did not return JSON"
                ) from error
            if (
                cli.returncode != 1
                or cli_result.get("state") != "failed"
                or "[TEST_CALLBACK_EXCEPTION] intentional E2E failure after record"
                not in cli_result.get("diagnostic", "")
                or (output_root / cli_result.get("execution_id", "missing")).exists()
                or (
                    output_root / f".{cli_result.get('execution_id', 'missing')}.work"
                ).exists()
            ):
                raise RuntimeError(
                    f"[MULTIBOT_CALLBACK_CLI_INVALID] exit={cli.returncode}, result={cli_result}, stderr={cli.stderr[-2000:]}"
                )
            continue
        if result["state"] != "succeeded":
            raise RuntimeError(f"[MULTIBOT_EXECUTION_FAILED] {result}")
        if build_manifest["playbook_id"] == "html-simple-catalog":
            html_entries.append(entry)
        example_id = {
            "html-simple-catalog": "html-simple",
            "synthetic-json-api": "json-api",
            "bounded-multipage-catalog": "multipage-catalog",
        }.get(build_manifest["playbook_id"])
        if (
            example_id == "html-simple"
            and build_manifest["source"]["source_revision"] == "fixture-2"
        ):
            example_id = "html-simple"
        if example_id is None:
            raise RuntimeError(
                f"[MULTIBOT_EXAMPLE_UNKNOWN] {build_manifest['playbook_id']}"
            )
        source_revision = build_manifest["source"]["source_revision"]
        _check_records(
            Path(result["output_dir"]),
            ROOT / "examples/playbooks" / example_id / "expected-records.jsonl",
            source_revision=source_revision if example_id == "html-simple" else None,
        )
    if len(html_entries) != 2 or html_entries[0]["bot_id"] != html_entries[1]["bot_id"]:
        raise RuntimeError(
            "[MULTIBOT_REVISION_MISSING] expected two builds of one logical bot"
        )

    # The second page is corrupted after page one emits records. No partial
    # result may be published, and a following different bot must still pass.
    multipage_entry = next(
        item
        for item in entries
        if item["bot_id"] != entries[0]["bot_id"]
        and json.loads(
            (assembled / "builds" / item["build_id"] / "manifest.json").read_text()
        )["playbook_id"]
        == "bounded-multipage-catalog"
    )
    multipage_index = json.loads(
        (
            assembled / "fixtures" / multipage_entry["build_id"] / "index.json"
        ).read_text()
    )
    second_page = multipage_index["pages"][1]
    target = (
        assembled / "fixtures" / multipage_entry["build_id"] / second_page["body_file"]
    )
    original = target.read_bytes()
    target.chmod(0o644)
    try:
        target.write_bytes(original + b"\n<!-- tampered -->")
        failed = run(
            catalog,
            multipage_entry["bot_id"],
            multipage_entry["build_id"],
            ExecutionContext(output_root),
        )
    finally:
        target.write_bytes(original)
        target.chmod(0o444)
    if failed["state"] != "failed" or list(
        output_root.glob(f"{failed['execution_id']}")
    ):
        raise RuntimeError(
            f"[MULTIBOT_FAILURE_ISOLATION] failed bot published output: {failed}"
        )
    unaffected = run(
        catalog,
        entries[3]["bot_id"],
        entries[3]["build_id"],
        ExecutionContext(output_root),
    )
    if unaffected["state"] != "succeeded":
        raise RuntimeError(
            f"[MULTIBOT_FAILURE_ISOLATION] following bot failed: {unaffected}"
        )

    timed_out = run(
        catalog,
        entries[3]["bot_id"],
        entries[3]["build_id"],
        ExecutionContext(output_root, timeout_seconds=0.0001),
    )
    if (
        timed_out["state"] != "timed_out"
        or (output_root / timed_out["execution_id"]).exists()
    ):
        raise RuntimeError(
            f"[MULTIBOT_TIMEOUT_INVALID] timeout output was published: {timed_out}"
        )


def _docker_acceptance(assembled: Path, entries: list[dict], temp: Path) -> None:
    tag = f"data-mozaic-scrapy-multibot-{uuid.uuid4().hex[:12]}"
    expected_assembly_id = json.loads(
        (assembled / "assembly.json").read_text(encoding="utf-8")
    )["assembly_id"]
    build_command = [
        "docker",
        "buildx",
        "build",
        "--platform",
        "linux/amd64",
        "--load",
        "--tag",
        tag,
        "--build-arg",
        f"CATALOG_ASSEMBLY_ID={expected_assembly_id}",
        "--build-context",
        f"botcatalog={assembled}",
        "--file",
        str(WORKER_DOCKERFILE),
        str(ROOT),
    ]
    image_created = False
    try:
        subprocess.run(build_command, check=True)
        image_created = True
        image_info = (
            subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    '{{.Id}}|{{index .Config.Labels "org.data-mozaic.scrapy-collector.assembly-id"}}',
                    tag,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .split("|")
        )
        if (
            len(image_info) != 2
            or not image_info[0].startswith("sha256:")
            or image_info[1] != expected_assembly_id
        ):
            raise RuntimeError(
                "[MULTIBOT_IMAGE_IDENTITY_MISMATCH] image label differs from assembly"
            )
        probe = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--platform",
                "linux/amd64",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65532:65532",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--pids-limit",
                "64",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--entrypoint",
                "python",
                tag,
                "-I",
                "-B",
                "-c",
                "import importlib.util, socket; assert importlib.util.find_spec('collector_engineering') is None;\ntry: socket.create_connection(('1.1.1.1', 443), timeout=1)\nexcept OSError: pass\nelse: raise AssertionError('network egress is available')",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        del probe
        output_root = temp / "docker-output"
        output_root.mkdir()
        common = [
            "docker",
            "run",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65532:65532",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m",
            tag,
        ]
        listing = subprocess.run(
            [*common[:2], "--rm", *common[2:], "catalog", "list"],
            check=True,
            capture_output=True,
            text=True,
        )
        catalog_doc = json.loads(listing.stdout)
        if len(catalog_doc["entries"]) != len(entries):
            raise RuntimeError(
                "[MULTIBOT_IMAGE_CATALOG_MISMATCH] image catalog differs"
            )
        for entry in entries:
            volume_name = f"scrapy-multibot-output-{uuid.uuid4().hex[:12]}"
            subprocess.run(
                ["docker", "volume", "create", volume_name],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                volume_mount = [
                    "--mount",
                    f"type=volume,src={volume_name},dst=/out",
                ]
                subprocess.run(
                    [
                        *common[:2],
                        "--rm",
                        *common[2:-1],
                        *volume_mount,
                        "--entrypoint",
                        "python",
                        tag,
                        "-I",
                        "-B",
                        "-c",
                        "import os; os.chown('/out', 65532, 65532); os.chmod('/out', 0o700)",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                execution = subprocess.run(
                    [
                        *common[:2],
                        "--rm",
                        *common[2:-1],
                        *volume_mount,
                        tag,
                        "run",
                        "--bot-id",
                        entry["bot_id"],
                        "--build-id",
                        entry["build_id"],
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                build_manifest = json.loads(
                    (
                        assembled / "builds" / entry["build_id"] / "manifest.json"
                    ).read_text()
                )
                is_callback_failure = (
                    build_manifest["source"]["source_revision"]
                    == "callback-failure-test"
                )
                if is_callback_failure:
                    try:
                        failed_result = json.loads(execution.stdout)
                    except json.JSONDecodeError as error:
                        raise RuntimeError(
                            "[MULTIBOT_CALLBACK_CONTAINER_INVALID] launcher did not return JSON"
                        ) from error
                    if (
                        execution.returncode != 1
                        or failed_result.get("state") != "failed"
                        or "[TEST_CALLBACK_EXCEPTION] intentional E2E failure after record"
                        not in failed_result.get("diagnostic", "")
                    ):
                        raise RuntimeError(
                            f"[MULTIBOT_CALLBACK_CONTAINER_INVALID] exit={execution.returncode}, result={failed_result}, stderr={execution.stderr[-2000:]}"
                        )
                    readonly_mount = [
                        "--mount",
                        f"type=volume,src={volume_name},dst=/out,readonly",
                    ]
                    inspect_output = subprocess.run(
                        [
                            *common[:2],
                            "--rm",
                            *common[2:-1],
                            *readonly_mount,
                            "--entrypoint",
                            "python",
                            tag,
                            "-I",
                            "-B",
                            "-c",
                            "import json,pathlib; print(json.dumps(sorted(p.name for p in pathlib.Path('/out').iterdir())))",
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    if json.loads(inspect_output.stdout) != []:
                        raise RuntimeError(
                            "[MULTIBOT_CALLBACK_CONTAINER_OUTPUT] failed crawl left published output"
                        )
                    continue
                if execution.returncode != 0:
                    raise RuntimeError(
                        f"[MULTIBOT_CONTAINER_EXIT] {execution.returncode}: "
                        f"{execution.stdout[-2000:]} {execution.stderr[-4000:]}"
                    )
                result = json.loads(execution.stdout)
                if result.get("state") != "succeeded":
                    raise RuntimeError(f"[MULTIBOT_CONTAINER_FAILED] {result}")
                read_output = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--platform",
                        "linux/amd64",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "65532:65532",
                        "--mount",
                        f"type=volume,src={volume_name},dst=/out,readonly",
                        "--entrypoint",
                        "python",
                        tag,
                        "-I",
                        "-B",
                        "-c",
                        "import base64,json,pathlib,sys; d=pathlib.Path('/out')/sys.argv[1]; print(json.dumps({n:base64.b64encode((d/n).read_bytes()).decode() for n in ('records.jsonl','report.json')}))",
                        result["execution_id"],
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                host_output = output_root / result["execution_id"]
                host_output.mkdir()
                for name, contents in json.loads(read_output.stdout).items():
                    (host_output / name).write_bytes(base64.b64decode(contents))
                manifest = json.loads(
                    (
                        assembled / "builds" / entry["build_id"] / "manifest.json"
                    ).read_text()
                )
                example_id = {
                    "html-simple-catalog": "html-simple",
                    "synthetic-json-api": "json-api",
                    "bounded-multipage-catalog": "multipage-catalog",
                }[manifest["playbook_id"]]
                _check_records(
                    host_output,
                    ROOT / "examples/playbooks" / example_id / "expected-records.jsonl",
                    source_revision=manifest["source"]["source_revision"]
                    if example_id == "html-simple"
                    else None,
                )
            finally:
                subprocess.run(
                    ["docker", "volume", "rm", "--force", volume_name],
                    check=False,
                    capture_output=True,
                )
    finally:
        if image_created:
            subprocess.run(
                ["docker", "image", "rm", "--force", tag],
                check=False,
                capture_output=True,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--docker",
        action="store_true",
        help="build and execute the restricted Linux image",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix=".scrapy-collector-multibot-", dir=ROOT
    ) as temporary:
        temp = Path(temporary)
        try:
            local_assembled, local_entries, _ = _prepare_catalog(
                temp / "local", runtime_platform_id()
            )
            _assert_browser_capability_rejected(temp)
            _local_execution_acceptance(local_assembled, local_entries, temp / "local")
            if args.docker:
                linux_assembled, linux_entries, _ = _prepare_catalog(
                    temp / "linux-amd64", "linux/amd64"
                )
                _docker_acceptance(linux_assembled, linux_entries, temp)
        finally:
            _make_tree_removable(temp)
    print("Scrapy Collector multi-bot conformance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
