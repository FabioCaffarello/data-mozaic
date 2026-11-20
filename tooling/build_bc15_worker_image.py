"""Build an exportable BC15 worker image from a pinned base and locked inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def source_digest() -> str:
    inputs = [
        ROOT / "uv.lock",
        ROOT / "apps/collector-engineering/pyproject.toml",
        ROOT / "apps/collector-engineering/Dockerfile.protected",
    ]
    inputs.extend(
        sorted(
            (ROOT / "apps/collector-engineering/collector_engineering").rglob("*.py")
        )
    )
    hash_value = hashlib.sha256()
    for path in inputs:
        hash_value.update(path.relative_to(ROOT).as_posix().encode() + b"\0")
        hash_value.update(bytes.fromhex(digest(path)))
    return hash_value.hexdigest()


def git_identity() -> tuple[str, str]:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
    )
    if status:
        raise SystemExit("worker release build requires a clean Git worktree")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True
    ).strip()
    return commit, tree


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-image", required=True, help="OCI image reference with @sha256 digest"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "@sha256:" not in args.base_image:
        parser.error("base image must be pinned with @sha256 digest")
    commit, git_tree = git_identity()
    output = args.output.resolve()
    if output.exists():
        parser.error("output directory already exists")
    output.mkdir(parents=True)
    docker = shutil.which("docker")
    if docker is None:
        parser.error("Docker is required to build the worker artifact")
    tag = "data-mozaic-bc15-release:" + source_digest()[:16]
    try:
        with tempfile.TemporaryDirectory(prefix="bc15-release-context-") as tmp:
            context = Path(tmp)
            subprocess.run(
                [
                    "uv",
                    "export",
                    "--locked",
                    "--package",
                    "mozaic-collector-engineering",
                    "--no-dev",
                    "--no-emit-workspace",
                    "--no-emit-package",
                    "mozaic-collector-engineering",
                    "--format",
                    "requirements.txt",
                    "--output-file",
                    str(context / "requirements.txt"),
                ],
                cwd=ROOT,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            shutil.copytree(
                ROOT / "apps/collector-engineering/collector_engineering",
                context / "collector_engineering",
            )
            shutil.copy2(
                ROOT / "apps/collector-engineering/Dockerfile.protected",
                context / "Dockerfile.protected",
            )
            subprocess.run(
                [
                    docker,
                    "build",
                    "--pull",
                    "--quiet",
                    "--build-arg",
                    f"PYTHON_BASE_IMAGE={args.base_image}",
                    "--tag",
                    tag,
                    "--file",
                    str(context / "Dockerfile.protected"),
                    str(context),
                ],
                cwd=ROOT,
                check=True,
                timeout=600,
            )
        image_id = subprocess.check_output(
            [docker, "image", "inspect", tag, "--format", "{{.Id}}"], text=True
        ).strip()
        if not image_id.startswith("sha256:") or len(image_id) != 71:
            raise SystemExit(
                "built worker image did not resolve to an immutable image ID"
            )
        archive = output / "bc15-worker-image.tar"
        with archive.open("wb") as stream:
            subprocess.run([docker, "save", image_id], check=True, stdout=stream)
        manifest = {
            "format": 1,
            "source_commit": commit,
            "git_tree": git_tree,
            "source_tree_digest_sha256": source_digest(),
            "uv_lock_sha256": digest(ROOT / "uv.lock"),
            "base_image": args.base_image,
            "worker_image_id": image_id,
            "archive_sha256": digest(archive),
        }
        (output / "worker-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        )
        print(json.dumps(manifest, sort_keys=True))
        return 0
    finally:
        subprocess.run(
            [docker, "image", "rm", "--force", tag], check=False, capture_output=True
        )


if __name__ == "__main__":
    raise SystemExit(main())
