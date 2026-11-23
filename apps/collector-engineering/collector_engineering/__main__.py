"""BC15 playbook authoring, compilation, and fixture-only execution CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .authoring_tools import inspect_fixture, try_selector
from .backend_python_http import check_capabilities, compile_build, verify_build
from .backend_scrapy_fixture import (
    check_capabilities as check_scrapy_capabilities,
)
from .backend_scrapy_fixture import (
    compile_build as compile_scrapy_build,
)
from .backend_scrapy_fixture import (
    verify_build as verify_scrapy_build,
)
from .playbook_v2 import build_typed_ir, load_playbook_v2, validate_playbook_v2
from .playbooks import build_draft, dump_yaml, validate_file
from .reference_runtime import RuntimeFailure
from .syntax import PlaybookError


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (55, 60))


def _run_network_isolated(command: list[str]) -> subprocess.CompletedProcess[str]:
    if sys.platform == "darwin":
        sandbox = shutil.which("sandbox-exec")
        if sandbox is None:
            raise PlaybookError(
                "[NETWORK_SANDBOX_UNAVAILABLE] sandbox-exec is required"
            )
        profile = "(version 1)(allow default)(deny network*)"
        return subprocess.run(
            [sandbox, "-p", profile, *command],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
            preexec_fn=_resource_limits,
        )
    docker = shutil.which("docker")
    if docker is not None:
        if len(command) >= 3 and command[1] == "-c":
            probe = [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--memory",
                "128m",
                "--pids-limit",
                "16",
                f"python:{sys.version_info.major}.{sys.version_info.minor}-slim",
                "python",
                "-c",
                command[2],
            ]
            return subprocess.run(
                probe, text=True, capture_output=True, check=False, timeout=10
            )
        build_bot = Path(command[1]).resolve()
        build_bot.parent.chmod(0o755)
        manifest_path = build_bot.parent / "manifest.json"
        try:
            build_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PlaybookError(f"[BUILD_MANIFEST_INVALID] {error}") from error
        scrapy_backend = build_manifest.get("backend_id") == "scrapy-fixture"
        fixtures = Path(command[command.index("--fixtures") + 1]).resolve()
        output = Path(command[command.index("--output") + 1]).resolve()
        if scrapy_backend:
            if output.exists() or output.is_symlink():
                raise PlaybookError(
                    "[OUTPUT_PATH_UNSAFE] Scrapy output destination must not exist"
                )
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            output.parent.chmod(0o777)
            docker_output = f"/output-parent/{output.name}"
            output_mount = output.parent
        else:
            output.mkdir(mode=0o700)
            output.chmod(0o777)
            docker_output = "/output"
            output_mount = output
        python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        site_packages = Path(sys.prefix) / "lib" / python_version / "site-packages"
        if not site_packages.is_dir():
            raise PlaybookError(
                "[NETWORK_SANDBOX_UNAVAILABLE] cannot locate locked Python runtime packages for Docker"
            )
        args = [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--memory",
            "768m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--ulimit",
            "cpu=60:60",
            "--user",
            "65534:65534",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=32m",
            "--mount",
            f"type=bind,src={site_packages},dst=/deps,readonly",
            "--mount",
            f"type=bind,src={build_bot.parent},dst=/build,readonly",
            "--mount",
            f"type=bind,src={fixtures},dst=/fixtures,readonly",
            "--mount",
            f"type=bind,src={output_mount},dst={'/output-parent' if scrapy_backend else '/output'}",
            "--env",
            "PYTHONPATH=/deps",
            f"python:{sys.version_info.major}.{sys.version_info.minor}-slim",
            "python",
            "-B",
            "/build/bot.py",
            "--fixtures",
            "/fixtures",
            "--output",
            docker_output,
        ]
        return subprocess.run(
            args, text=True, capture_output=True, check=False, timeout=60
        )
    raise PlaybookError(
        "[NETWORK_SANDBOX_UNAVAILABLE] Linux execution requires Docker with --network none"
    )


def _run_example(example: Path) -> int:
    playbook_path = example / "capture-playbook.yaml"
    fixture_root = example / "fixtures"
    expected_path = example / "expected-records.jsonl"
    playbook = load_playbook_v2(playbook_path)
    result = validate_playbook_v2(playbook, fixture_root=fixture_root)
    _json(
        {
            "validation": "passed" if result.valid else "failed",
            "evidence_fixture_coverage": result.evidence_sufficient,
            "diagnostics": [item.as_dict() for item in result.diagnostics],
        }
    )
    if not result.valid:
        return 1
    with tempfile.TemporaryDirectory(prefix="bc15-example-") as temp:
        temp_root = Path(temp).resolve()
        build_dir = temp_root / "build"
        try:
            manifest = compile_build(
                playbook,
                fixture_root=fixture_root,
                output=build_dir,
                lockfile=_repo_root() / "uv.lock",
            )
        except PlaybookError as error:
            message = str(error)
            if "CAPABILITY_UNSUPPORTED" in message:
                _json({"compile": "rejected-as-expected", "diagnostic": message})
                return 0
            print(message, file=sys.stderr)
            return 1
        verified = verify_build(build_dir)
        if verified["build_id"] != manifest["build_id"]:
            print(
                "[BUILD_ID_MISMATCH] verifier disagreed with compiler", file=sys.stderr
            )
            return 1
        _json(
            {
                "compile": "passed",
                "build_id": manifest["build_id"],
                "ir_sha256": manifest["ir_sha256"],
            }
        )
        output = temp_root / "output"
        command = [
            sys.executable,
            str(build_dir / "bot.py"),
            "--fixtures",
            str(fixture_root),
            "--output",
            str(output),
        ]
        try:
            completed = _run_network_isolated(command)
        except subprocess.TimeoutExpired as error:
            raise PlaybookError(
                "[EXECUTION_TIMEOUT] offline example exceeded 60 seconds"
            ) from error
        print(completed.stdout, end="")
        if completed.returncode:
            print(completed.stderr, file=sys.stderr, end="")
            return completed.returncode
        if expected_path.is_file():
            actual = output / "records.jsonl"
            if actual.read_bytes() != expected_path.read_bytes():
                _json(
                    {
                        "comparison": "failed",
                        "expected": str(expected_path),
                        "actual_sha256": hashlib.sha256(
                            actual.read_bytes()
                        ).hexdigest(),
                    }
                )
                return 1
            _json(
                {
                    "comparison": "passed",
                    "records": sum(1 for _ in actual.open("rb")),
                    "build_id": manifest["build_id"],
                }
            )
        return 0


def _run_cross_backend_conformance(examples_root: Path) -> int:
    """Compile both backends from one IR and compare them to independent oracles."""
    try:
        if _verify_network_sandbox() != 0:
            return 1
        names = ("html-simple", "multipage-catalog", "json-api")
        with tempfile.TemporaryDirectory(prefix="bc15-conformance-") as temporary:
            root = Path(temporary).resolve()
            for name in names:
                example = examples_root / name
                fixture_root = example / "fixtures"
                parsed = load_playbook_v2(example / "capture-playbook.yaml")
                validation = validate_playbook_v2(parsed, fixture_root=fixture_root)
                if not validation.valid:
                    raise PlaybookError(
                        f"[CONFORMANCE_VALIDATION_FAILED] {name}: "
                        + "; ".join(item.code for item in validation.diagnostics)
                    )
                ir = build_typed_ir(parsed, fixture_root=fixture_root)
                builds = {}
                outputs = {}
                reports = {}
                for backend in ("python-http", "scrapy-fixture"):
                    build_dir = root / f"{name}-{backend}-build"
                    if backend == "python-http":
                        manifest = compile_build(
                            parsed,
                            fixture_root=fixture_root,
                            output=build_dir,
                            lockfile=_repo_root() / "uv.lock",
                        )
                        verified = verify_build(build_dir)
                    else:
                        scrapy_ir = build_typed_ir(parsed, fixture_root=fixture_root)
                        if scrapy_ir.canonical_bytes() != ir.canonical_bytes():
                            raise PlaybookError(
                                "[CONFORMANCE_IR_MISMATCH] backend inputs differ"
                            )
                        manifest = compile_scrapy_build(
                            scrapy_ir,
                            fixture_root=fixture_root,
                            output=build_dir,
                            lockfile=_repo_root() / "uv.lock",
                        )
                        verified = verify_scrapy_build(build_dir)
                    if (
                        manifest["build_id"] != verified["build_id"]
                        or manifest["ir_sha256"] != ir.sha256
                    ):
                        raise PlaybookError(
                            f"[CONFORMANCE_BUILD_INVALID] {name}/{backend}"
                        )
                    builds[backend] = manifest
                    output = root / f"{name}-{backend}-output"
                    command = [
                        sys.executable,
                        "-B",
                        str(build_dir / "bot.py"),
                        "--fixtures",
                        str(fixture_root),
                        "--output",
                        str(output),
                    ]
                    try:
                        completed = _run_network_isolated(command)
                    except subprocess.TimeoutExpired as error:
                        raise PlaybookError(
                            f"[CONFORMANCE_TIMEOUT] {name}/{backend} exceeded 60 seconds"
                        ) from error
                    if completed.returncode != 0:
                        raise PlaybookError(
                            f"[CONFORMANCE_EXECUTION_FAILED] {name}/{backend}: "
                            f"{completed.stderr.strip() or completed.stdout.strip()}"
                        )
                    records_path = output / "records.jsonl"
                    if not records_path.is_file():
                        raise PlaybookError(
                            f"[CONFORMANCE_OUTPUT_MISSING] {name}/{backend}"
                        )
                    outputs[backend] = records_path.read_bytes()
                    reports[backend] = json.loads(
                        (output / "report.json").read_text(encoding="utf-8")
                    )
                expected = (example / "expected-records.jsonl").read_bytes()
                if outputs["python-http"] != expected:
                    raise PlaybookError(f"[ORACLE_MISMATCH] {name}/python-http")
                if outputs["scrapy-fixture"] != expected:
                    raise PlaybookError(f"[ORACLE_MISMATCH] {name}/scrapy-fixture")
                if outputs["python-http"] != outputs["scrapy-fixture"]:
                    raise PlaybookError(f"[BACKEND_RECORDS_MISMATCH] {name}")
                if (
                    builds["python-http"]["build_id"]
                    == builds["scrapy-fixture"]["build_id"]
                ):
                    raise PlaybookError(f"[BACKEND_BUILD_ID_COLLISION] {name}")
                common = (
                    "state",
                    "pages",
                    "requests",
                    "response_bytes",
                    "records",
                    "records_sha256",
                )
                if any(
                    reports["python-http"].get(key)
                    != reports["scrapy-fixture"].get(key)
                    for key in common
                ):
                    raise PlaybookError(f"[BACKEND_REPORT_MISMATCH] {name}")
                _json(
                    {
                        "conformance": "passed",
                        "example": name,
                        "ir_sha256": ir.sha256,
                        "build_ids": {
                            key: value["build_id"] for key, value in builds.items()
                        },
                        "records": reports["python-http"]["records"],
                        "provenance_and_order": "identical",
                        "network": "denied by platform runner",
                    }
                )

            negative_example = examples_root / "multipage-catalog"
            negative_parsed = load_playbook_v2(
                negative_example / "capture-playbook.yaml"
            )
            negative_ir = build_typed_ir(
                negative_parsed, fixture_root=negative_example / "fixtures"
            )
            negative_build = root / "scrapy-negative-build"
            compile_scrapy_build(
                negative_ir,
                fixture_root=negative_example / "fixtures",
                output=negative_build,
                lockfile=_repo_root() / "uv.lock",
            )
            verify_scrapy_build(negative_build)
            fixture_index = json.loads(
                (negative_example / "fixtures/index.json").read_text()
            )
            page_two = next(
                item
                for item in fixture_index["pages"]
                if item["url"].endswith("/page-2")
            )
            body_path = page_two["body_file"]
            for failure_case in (
                "fixture-tampered",
                "fixture-missing",
                "partial-output",
            ):
                case_root = root / failure_case
                case_root.mkdir()
                case_fixtures = case_root / "fixtures"
                shutil.copytree(negative_example / "fixtures", case_fixtures)
                case_body = case_fixtures / body_path
                if failure_case == "fixture-tampered":
                    case_body.write_bytes(case_body.read_bytes() + b"tampered")
                else:
                    case_body.unlink()
                output = case_root / "output"
                command = [
                    sys.executable,
                    "-B",
                    str(negative_build / "bot.py"),
                    "--fixtures",
                    str(case_fixtures),
                    "--output",
                    str(output),
                ]
                completed = _run_network_isolated(command)
                if completed.returncode == 0:
                    raise PlaybookError(
                        f"[CONFORMANCE_FAILURE_NOT_PROPAGATED] {failure_case}"
                    )
                if output.exists():
                    raise PlaybookError(
                        f"[CONFORMANCE_PARTIAL_OUTPUT_PUBLISHED] {failure_case}"
                    )
                _json(
                    {
                        "conformance": "passed",
                        "failure_case": failure_case,
                        "runner_exit": completed.returncode,
                        "output_published": False,
                    }
                )

            browser = examples_root / "browser-capability-unsupported"
            parsed = load_playbook_v2(browser / "capture-playbook.yaml")
            ir = build_typed_ir(parsed, fixture_root=browser / "fixtures")
            for backend, checker in (
                ("python-http", check_capabilities),
                ("scrapy-fixture", check_scrapy_capabilities),
            ):
                missing = checker(ir)
                if not any(
                    item["capability"] == "browser.rendered_dom" for item in missing
                ):
                    raise PlaybookError(
                        f"[CONFORMANCE_CAPABILITY_FAILURE] {backend} accepted browser capability"
                    )
            _json(
                {
                    "conformance": "passed",
                    "example": "browser-capability-unsupported",
                    "result": "CAPABILITY_UNSUPPORTED",
                }
            )
        return 0
    except (OSError, json.JSONDecodeError, RuntimeFailure, PlaybookError) as error:
        print(str(error), file=sys.stderr)
        return 1


def _verify_network_sandbox() -> int:
    probe = """
import errno, socket, sys
try:
    socket.create_connection(("198.51.100.1", 80), timeout=2)
except OSError as error:
    sys.exit(0 if error.errno in {errno.EPERM, errno.EACCES, errno.ENETUNREACH, errno.EHOSTUNREACH} else 3)
sys.exit(1)
"""
    try:
        result = _run_network_isolated([sys.executable, "-c", probe])
    except subprocess.TimeoutExpired as error:
        raise PlaybookError(
            "[NETWORK_SANDBOX_PROBE_TIMEOUT] socket denial probe timed out"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        print(
            f"[NETWORK_SANDBOX_PROBE_FAILED] socket connection was not denied: {detail}",
            file=sys.stderr,
        )
        return 1
    print("network sandbox denied the external socket probe")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="collector-engineering")
    subparsers = parser.add_subparsers(dest="command", required=True)
    draft_parser = subparsers.add_parser(
        "draft", help="create a v1 YAML draft from a BC14 transcript"
    )
    draft_parser.add_argument("transcript", type=Path)
    draft_parser.add_argument("--output", type=Path, required=True)
    validate_parser = subparsers.add_parser(
        "validate", help="validate a v1 or v2 playbook"
    )
    validate_parser.add_argument("playbook", type=Path)
    validate_parser.add_argument("--fixtures", type=Path)
    inspect_parser = subparsers.add_parser(
        "inspect-fixture", help="inspect fixture metadata and minimized structure"
    )
    inspect_parser.add_argument("fixture_root", type=Path)
    inspect_parser.add_argument("url")
    selector_parser = subparsers.add_parser(
        "try-selector", help="test a CSS selector against one verified fixture"
    )
    selector_parser.add_argument("fixture_root", type=Path)
    selector_parser.add_argument("url")
    selector_parser.add_argument("selector")
    selector_parser.add_argument("--evidence-ref", required=True)
    selector_parser.add_argument("--max-samples", type=int, default=3)
    compile_parser = subparsers.add_parser(
        "compile", help="compile a validated v2 playbook"
    )
    compile_parser.add_argument("playbook", type=Path)
    compile_parser.add_argument(
        "--backend", choices=["python-http", "scrapy-fixture"], required=True
    )
    compile_parser.add_argument("--fixtures", type=Path, required=True)
    compile_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser(
        "verify-build", help="verify BotBuild files and digests"
    )
    verify_parser.add_argument("build_dir", type=Path)
    run_parser = subparsers.add_parser(
        "run-example",
        help="compile, execute in a no-network sandbox, and compare an example",
    )
    run_parser.add_argument("example", type=Path)
    conformance_parser = subparsers.add_parser(
        "cross-backend-conformance",
        help="run Python Reference and Scrapy fixture backends against independent oracles",
    )
    conformance_parser.add_argument(
        "--examples", type=Path, default=_repo_root() / "examples/playbooks"
    )
    subparsers.add_parser(
        "verify-network-sandbox", help="prove the OS runner denies an external socket"
    )
    compare_parser = subparsers.add_parser(
        "compare", help="compare JSONL output with independent expected records"
    )
    compare_parser.add_argument("expected", type=Path)
    compare_parser.add_argument("actual", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "draft":
            transcript = json.loads(args.transcript.read_text(encoding="utf-8"))
            candidate = build_draft(transcript)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(dump_yaml(candidate), encoding="utf-8")
            print(f"wrote draft {candidate['playbook_id']} to {args.output}")
            return 0
        if args.command == "validate":
            if args.playbook.suffix.lower() in {".yaml", ".yml"}:
                parsed = load_playbook_v2(args.playbook)
                if parsed.document.get("dsl_version") != "capture-playbook-draft/v2":
                    errors = validate_file(args.playbook)
                    if errors:
                        for error in errors:
                            print(f"[V1_INVALID] {error}", file=sys.stderr)
                        return 1
                    print("CapturePlaybook draft/v1 is schema and semantically valid")
                    return 0
                result = validate_playbook_v2(parsed, fixture_root=args.fixtures)
                _json(
                    {
                        "valid": result.valid,
                        "evidence_fixture_coverage": result.evidence_sufficient,
                        "diagnostics": [item.as_dict() for item in result.diagnostics],
                    }
                )
                return 0 if result.valid else 1
            errors = validate_file(args.playbook)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print("CapturePlaybook draft/v1 is schema and semantically valid")
            return 0
        if args.command == "inspect-fixture":
            _json(inspect_fixture(args.fixture_root, args.url))
            return 0
        if args.command == "try-selector":
            result = try_selector(
                args.fixture_root,
                args.url,
                args.selector,
                evidence_ref=args.evidence_ref,
                max_samples=args.max_samples,
            )
            _json(result)
            return 0 if result.get("valid_selector") else 1
        if args.command == "compile":
            parsed = load_playbook_v2(args.playbook)
            result = validate_playbook_v2(parsed, fixture_root=args.fixtures)
            if not result.valid:
                _json(
                    {
                        "valid": False,
                        "diagnostics": [item.as_dict() for item in result.diagnostics],
                    }
                )
                return 1
            if args.backend == "python-http":
                manifest = compile_build(
                    parsed,
                    fixture_root=args.fixtures,
                    output=args.output,
                    lockfile=_repo_root() / "uv.lock",
                )
            else:
                ir = build_typed_ir(parsed, fixture_root=args.fixtures)
                manifest = compile_scrapy_build(
                    ir,
                    fixture_root=args.fixtures,
                    output=args.output,
                    lockfile=_repo_root() / "uv.lock",
                )
            _json(
                {
                    "build_id": manifest["build_id"],
                    "manifest": str(args.output / "manifest.json"),
                }
            )
            return 0
        if args.command == "verify-build":
            try:
                raw_manifest = json.loads(
                    (args.build_dir / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as error:
                raise PlaybookError(f"[BUILD_MANIFEST_INVALID] {error}") from error
            if raw_manifest.get("backend_id") == "scrapy-fixture":
                manifest = verify_scrapy_build(args.build_dir)
            else:
                manifest = verify_build(args.build_dir)
            _json(
                {
                    "valid": True,
                    "build_id": manifest["build_id"],
                    "generated_files": len(manifest["generated_files"]),
                }
            )
            return 0
        if args.command == "run-example":
            return _run_example(args.example)
        if args.command == "cross-backend-conformance":
            return _run_cross_backend_conformance(args.examples)
        if args.command == "verify-network-sandbox":
            return _verify_network_sandbox()
        if args.command == "compare":
            expected = args.expected.read_bytes()
            actual = args.actual.read_bytes()
            if expected != actual:
                print(
                    "[RECORDS_MISMATCH] JSONL differs from independent expectations",
                    file=sys.stderr,
                )
                return 1
            print(f"records match ({sum(1 for line in actual.splitlines() if line)})")
            return 0
    except (OSError, json.JSONDecodeError, RuntimeFailure, PlaybookError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
