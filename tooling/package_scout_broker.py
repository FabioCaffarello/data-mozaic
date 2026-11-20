"""Create a target-native, hash-manifested Scout broker bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

try:
    from .scout_release_artifact import (
        architecture_for_platform,
        archive_image_config,
        sha256_file,
        verify_wheelhouse,
    )
except ImportError:  # direct execution via `python tooling/package_scout_broker.py`
    from scout_release_artifact import (
        architecture_for_platform,
        archive_image_config,
        sha256_file,
        verify_wheelhouse,
    )

ROOT = Path(__file__).resolve().parents[1]


def source_digest() -> str:
    files = [
        ROOT / "uv.lock",
        ROOT / "apps/scout/pyproject.toml",
        ROOT / "libs/python/observability/pyproject.toml",
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
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def git_identity() -> tuple[str, str]:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
    )
    if status:
        raise SystemExit("broker release build requires a clean Git worktree")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True
    ).strip()
    return commit, tree


def native_platform() -> str:
    if sys.platform != "linux":
        raise SystemExit(
            "release broker bundles must be built on a native Linux runner"
        )
    machines = {"x86_64": "amd64", "aarch64": "arm64"}
    architecture = machines.get(platform.machine().lower())
    if architecture is None:
        raise SystemExit("unsupported native Linux broker architecture")
    return f"linux/{architecture}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-bundle", type=Path, required=True)
    parser.add_argument(
        "--target-platform", required=True, choices=("linux/amd64", "linux/arm64")
    )
    args = parser.parse_args()
    if native_platform() != args.target_platform:
        parser.error("broker wheels must be resolved on the target Linux architecture")
    expected_os, expected_arch = architecture_for_platform(args.target_platform)
    commit, git_tree = git_identity()
    worker_root = args.worker_bundle.resolve(strict=True)
    worker_manifest_path = worker_root / "worker-manifest.json"
    worker_archive = worker_root / "bc15-worker-image.tar"
    dockerfile = worker_root / "Dockerfile.protected"
    for artifact in (worker_manifest_path, worker_archive, dockerfile):
        if not artifact.is_file() or artifact.is_symlink():
            parser.error(f"worker artifact is missing or unsafe: {artifact.name}")
    worker_manifest = json.loads(worker_manifest_path.read_text(encoding="utf-8"))
    if (
        worker_manifest.get("source_commit") != commit
        or worker_manifest.get("git_tree") != git_tree
        or worker_manifest.get("requested_platform") != args.target_platform
    ):
        parser.error(
            "worker image must match this exact clean commit and target platform"
        )
    if sha256_file(
        ROOT / "apps/collector-engineering/Dockerfile.protected"
    ) != sha256_file(dockerfile):
        parser.error("worker Dockerfile does not match this source revision")
    if sha256_file(worker_archive) != worker_manifest.get("archive_sha256"):
        parser.error("worker archive digest does not match its manifest")
    try:
        image_metadata = archive_image_config(worker_archive)
    except ValueError as error:
        parser.error(str(error))
    image_id = worker_manifest.get("worker_image_id")
    if (
        image_id != worker_manifest.get("build_image_id")
        or image_id != worker_manifest.get("archive_config_digest")
        or image_id != worker_manifest.get("imported_image_id")
        or image_id != image_metadata["config_digest"]
        or image_metadata["os"] != expected_os
        or image_metadata["architecture"] != expected_arch
    ):
        parser.error(
            "worker image IDs, archive config digest, or target platform differ"
        )
    output = args.output.resolve()
    if output.exists():
        parser.error(
            "output directory already exists; choose a new artifact destination"
        )

    with tempfile.TemporaryDirectory(prefix="scout-linux-package-") as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        requirements = staging / "requirements.txt"
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
        wheels = staging / "wheels"
        wheels.mkdir()
        for package in ("mozaic-observability", "mozaic-scout"):
            subprocess.run(
                [
                    "uv",
                    "build",
                    "--package",
                    package,
                    "--wheel",
                    "--out-dir",
                    str(wheels),
                ],
                cwd=ROOT,
                check=True,
            )
        shutil.copy2(ROOT / "uv.lock", staging / "uv.lock")
        shutil.copy2(
            ROOT / "apps/collector-engineering/Dockerfile.protected",
            staging / "Dockerfile.protected",
        )
        shutil.copy2(worker_archive, staging / worker_archive.name)
        shutil.copy2(worker_manifest_path, staging / worker_manifest_path.name)
        shutil.copy2(ROOT / "tooling/verify_scout_bundle.py", staging)
        shutil.copy2(ROOT / "tooling/scout_release_artifact.py", staging)
        shutil.copy2(ROOT / "tooling/install_scout_bundle.sh", staging)
        (staging / "install_scout_bundle.sh").chmod(0o755)
        wheelhouse = staging / "wheelhouse"
        wheelhouse.mkdir()
        with tempfile.TemporaryDirectory(
            prefix="scout-broker-wheel-download-"
        ) as venv_dir:
            venv = Path(venv_dir) / "venv"
            subprocess.run(
                ["uv", "venv", "--python", "3.12", "--seed", str(venv)], check=True
            )
            python = venv / "bin/python"
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--require-hashes",
                    "--requirement",
                    str(requirements),
                    "--dest",
                    str(wheelhouse),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        verify_wheelhouse(wheelhouse, args.target_platform)

        lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
        sdk = next(
            package["version"]
            for package in lock["package"]
            if package["name"] == "mcp"
        )
        manifest = {
            "format": 2,
            "artifact": "data-mozaic-scout-trusted-mcp-broker",
            "source_commit": commit,
            "git_tree": git_tree,
            "source_tree_digest_sha256": source_digest(),
            "uv_lock_sha256": sha256_file(ROOT / "uv.lock"),
            "requirements_sha256": sha256_file(requirements),
            "dockerfile_sha256": sha256_file(staging / "Dockerfile.protected"),
            "python": platform.python_version(),
            "target_platform": args.target_platform,
            "platform": platform.platform(),
            "mcp_sdk": sdk,
            "worker_image_id": image_id,
            "worker_manifest_sha256": sha256_file(worker_manifest_path),
            "worker_archive_sha256": sha256_file(worker_archive),
            "worker_archive_config_digest": image_metadata["config_digest"],
            "repo_digests": worker_manifest["repo_digests"],
            "files": {},
        }
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                manifest["files"][path.relative_to(staging).as_posix()] = sha256_file(
                    path
                )
        (staging / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, output)
    print(output)
    print(
        json.dumps(
            {
                "source_commit": commit,
                "git_tree": git_tree,
                "target_platform": args.target_platform,
                "manifest_sha256": sha256_file(output / "manifest.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
