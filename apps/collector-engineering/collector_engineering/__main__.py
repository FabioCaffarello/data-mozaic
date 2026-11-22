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
from .backend_python_http import compile_build, verify_build
from .playbook_v2 import load_playbook_v2, validate_playbook_v2
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
        fixtures = Path(command[command.index("--fixtures") + 1]).resolve()
        output = Path(command[command.index("--output") + 1]).resolve()
        output.mkdir(mode=0o700)
        output.chmod(0o777)
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
            f"type=bind,src={output},dst=/output",
            "--env",
            "PYTHONPATH=/deps",
            f"python:{sys.version_info.major}.{sys.version_info.minor}-slim",
            "python",
            "-B",
            "/build/bot.py",
            "--fixtures",
            "/fixtures",
            "--output",
            "/output",
        ]
        return subprocess.run(
            args, text=True, capture_output=True, check=False, timeout=60
        )
    unshare = shutil.which("unshare")
    if unshare is None:
        raise PlaybookError(
            "[NETWORK_SANDBOX_UNAVAILABLE] Linux execution requires Docker --network none or unshare -n"
        )
    return subprocess.run(
        [unshare, "--net", "--", *command],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        preexec_fn=_resource_limits,
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
    compile_parser.add_argument("--backend", choices=["python-http"], required=True)
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
            manifest = compile_build(
                parsed,
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
