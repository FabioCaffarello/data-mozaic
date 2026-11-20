"""Create a platform-specific, hash-manifested broker install bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_digest() -> str:
    files = [
        ROOT / "uv.lock",
        ROOT / "apps/scout/pyproject.toml",
        ROOT / "apps/collector-engineering/pyproject.toml",
        ROOT / "apps/collector-engineering/Dockerfile.protected",
    ]
    files.extend(sorted((ROOT / "apps/scout/scout").rglob("*.py")))
    files.extend(sorted((ROOT / "libs/python/observability").rglob("*.py")))
    files.extend(
        sorted(
            (ROOT / "apps/collector-engineering/collector_engineering").rglob("*.py")
        )
    )
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-bundle", type=Path, required=True)
    args = parser.parse_args()
    worker_manifest_path = args.worker_bundle.resolve() / "worker-manifest.json"
    worker_archive = args.worker_bundle.resolve() / "bc15-worker-image.tar"
    if not worker_manifest_path.is_file() or not worker_archive.is_file():
        parser.error(
            "--worker-bundle must contain worker-manifest.json and bc15-worker-image.tar"
        )
    worker_manifest = json.loads(worker_manifest_path.read_text(encoding="utf-8"))
    if (
        not worker_manifest["worker_image_id"].startswith("sha256:")
        or len(worker_manifest["worker_image_id"]) != 71
    ):
        parser.error("worker manifest must identify an immutable sha256 image ID")
    if sha256(worker_archive) != worker_manifest["archive_sha256"]:
        parser.error("worker archive digest does not match its manifest")
    output = args.output.resolve()
    if output.exists():
        parser.error(
            "output directory already exists; choose a new artifact destination"
        )
    output.mkdir(parents=True)
    wheels = output / "wheels"
    wheels.mkdir()
    requirements = output / "requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--package",
            "mozaic-scout",
            "--no-dev",
            "--no-emit-workspace",
            "--no-emit-package",
            "mozaic-scout",
            "--format",
            "requirements.txt",
            "--output-file",
            str(requirements),
        ],
        cwd=ROOT,
        check=True,
    )
    for package in ("mozaic-observability", "mozaic-scout"):
        subprocess.run(
            ["uv", "build", "--package", package, "--wheel", "--out-dir", str(wheels)],
            cwd=ROOT,
            check=True,
        )
    shutil.copy2(
        ROOT / "tooling/verify_scout_bundle.py", output / "verify_scout_bundle.py"
    )
    shutil.copy2(
        ROOT / "tooling/install_scout_bundle.sh", output / "install_scout_bundle.sh"
    )
    (output / "install_scout_bundle.sh").chmod(0o755)
    shutil.copy2(worker_archive, output / "bc15-worker-image.tar")
    shutil.copy2(worker_manifest_path, output / "worker-manifest.json")
    wheelhouse = output / "wheelhouse"
    wheelhouse.mkdir()
    with tempfile.TemporaryDirectory(prefix="scout-broker-packager-") as temporary:
        venv = Path(temporary) / "venv"
        subprocess.run(
            ["uv", "venv", "--python", "3.12", "--seed", str(venv)],
            cwd=ROOT,
            check=True,
        )
        python = venv / "bin/python"
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "download",
                "--require-hashes",
                "--requirement",
                str(requirements),
                "--dest",
                str(wheelhouse),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    sdk = next(
        package["version"] for package in lock["package"] if package["name"] == "mcp"
    )
    manifest = {
        "format": 1,
        "artifact": "data-mozaic-scout-trusted-mcp-broker",
        "source_commit": commit,
        "source_tree_digest_sha256": source_digest(),
        "uv_lock_sha256": sha256(ROOT / "uv.lock"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "mcp_sdk": sdk,
        "worker_image_id": worker_manifest["worker_image_id"],
        "worker_manifest_sha256": sha256(worker_manifest_path),
        "files": {},
    }
    for path in sorted(output.rglob("*")):
        if path.is_file():
            manifest["files"][path.relative_to(output).as_posix()] = sha256(path)
    (output / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    print(output)
    print(
        json.dumps(
            {
                "source_commit": commit,
                "source_digest": manifest["source_tree_digest_sha256"],
                "manifest_sha256": sha256(output / "manifest.json"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
