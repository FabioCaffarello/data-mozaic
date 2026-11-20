"""Build and round-trip a platform-specific BC15 image in disposable Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

try:
    from .scout_release_artifact import (
        architecture_for_platform,
        archive_image_config,
        runtime_config_differences,
    )
except ImportError:  # direct execution via `python tooling/build_bc15_worker_image.py`
    from scout_release_artifact import (
        architecture_for_platform,
        archive_image_config,
        runtime_config_differences,
    )

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


def docker_command(binary: str, endpoint: str, *args: str) -> list[str]:
    return [binary, "--host", endpoint, *args]


def inspect_image(binary: str, endpoint: str, reference: str) -> dict:
    output = subprocess.check_output(
        docker_command(
            binary,
            endpoint,
            "image",
            "inspect",
            reference,
            "--format",
            "{{json .}}",
        ),
        text=True,
    )
    return json.loads(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-image", required=True, help="OCI image reference with @sha256 digest"
    )
    parser.add_argument(
        "--platform", required=True, choices=("linux/amd64", "linux/arm64")
    )
    parser.add_argument("--docker-host", required=True, help="disposable build daemon")
    parser.add_argument(
        "--verify-docker-host", required=True, help="disposable image-import daemon"
    )
    parser.add_argument(
        "--candidate-record",
        type=Path,
        help="write the exact image ID for scoped disposable-runner cleanup",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if "@sha256:" not in args.base_image:
        parser.error("base image must be pinned with @sha256 digest")
    expected_os, expected_arch = architecture_for_platform(args.platform)
    commit, git_tree = git_identity()
    output = args.output.resolve()
    if output.exists():
        parser.error("output directory already exists")
    docker = shutil.which("docker")
    if docker is None:
        parser.error("Docker CLI is required to build the worker artifact")

    source = source_digest()
    tag = f"data-mozaic-bc15-release:{commit[:12]}-{expected_arch}-{uuid.uuid4().hex[:12]}"
    imported_image_id: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="bc15-release-") as temporary:
            temp = Path(temporary)
            context = temp / "context"
            context.mkdir()
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
            dockerfile = ROOT / "apps/collector-engineering/Dockerfile.protected"
            shutil.copy2(dockerfile, context / "Dockerfile.protected")
            subprocess.run(
                docker_command(
                    docker,
                    args.docker_host,
                    "image",
                    "pull",
                    "--platform",
                    args.platform,
                    args.base_image,
                ),
                check=True,
                timeout=600,
            )
            base = inspect_image(docker, args.docker_host, args.base_image)
            if (
                base.get("Os") != expected_os
                or base.get("Architecture") != expected_arch
            ):
                raise SystemExit("pinned base image platform does not match request")
            subprocess.run(
                docker_command(
                    docker,
                    args.docker_host,
                    "build",
                    "--pull",
                    "--quiet",
                    "--platform",
                    args.platform,
                    "--build-arg",
                    f"PYTHON_BASE_IMAGE={args.base_image}",
                    "--tag",
                    tag,
                    "--file",
                    str(context / "Dockerfile.protected"),
                    str(context),
                ),
                cwd=ROOT,
                check=True,
                timeout=900,
            )

            built = inspect_image(docker, args.docker_host, tag)
            image_id = built["Id"]
            if not image_id.startswith("sha256:") or len(image_id) != 71:
                raise SystemExit("Docker returned an invalid worker image ID")
            if (
                built.get("Os") != expected_os
                or built.get("Architecture") != expected_arch
            ):
                raise SystemExit("built worker image platform does not match request")
            archive = temp / "bc15-worker-image.tar"
            with archive.open("wb") as stream:
                subprocess.run(
                    docker_command(docker, args.docker_host, "image", "save", image_id),
                    check=True,
                    stdout=stream,
                    timeout=600,
                )
            archive_meta = archive_image_config(archive)
            if archive_meta["config_digest"] != image_id:
                raise SystemExit(
                    "exported Docker archive config digest differs from build image ID"
                )
            if (
                archive_meta["os"] != expected_os
                or archive_meta["architecture"] != expected_arch
            ):
                raise SystemExit("exported Docker archive has the wrong platform")
            expected_runtime = built.get("Config", {})
            runtime_differences = runtime_config_differences(
                archive_meta["runtime_config"], expected_runtime
            )
            if runtime_differences:
                raise SystemExit(
                    "exported image runtime config differs in: "
                    + ", ".join(runtime_differences)
                )

            build_tags = built.get("RepoTags") or []
            if build_tags != [tag]:
                raise SystemExit(
                    "refusing to remove a candidate image with unexpected Docker tags"
                )
            if args.candidate_record:
                args.candidate_record.parent.mkdir(parents=True, exist_ok=True)
                args.candidate_record.write_text(image_id + "\n", encoding="ascii")
            if args.docker_host == args.verify_docker_host:
                # The explicitly selected CI daemon is disposable. Remove only
                # the uniquely tagged build result so image load proves a round trip.
                subprocess.run(
                    docker_command(docker, args.docker_host, "image", "rm", image_id),
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
            else:
                subprocess.run(
                    docker_command(docker, args.docker_host, "image", "rm", tag),
                    check=True,
                    capture_output=True,
                    timeout=60,
                )
            # Import must start from an absent image in the disposable verifier.
            existing = subprocess.run(
                docker_command(
                    docker,
                    args.verify_docker_host,
                    "image",
                    "inspect",
                    image_id,
                    "--format",
                    "{{.Id}}",
                ),
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            if existing.returncode == 0:
                raise SystemExit(
                    "verification daemon already contains the candidate image"
                )
            subprocess.run(
                docker_command(
                    docker,
                    args.verify_docker_host,
                    "image",
                    "load",
                    "--input",
                    str(archive),
                ),
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
            imported = inspect_image(docker, args.verify_docker_host, image_id)
            imported_image_id = imported["Id"]
            if (
                imported_image_id != image_id
                or imported.get("Os") != expected_os
                or imported.get("Architecture") != expected_arch
            ):
                raise SystemExit("imported worker image identity or platform differs")
            runtime_differences = runtime_config_differences(
                imported.get("Config", {}), expected_runtime
            )
            if runtime_differences:
                raise SystemExit(
                    "imported image runtime config differs in: "
                    + ", ".join(runtime_differences)
                )

            if args.docker_host != args.verify_docker_host:
                subprocess.run(
                    docker_command(docker, args.docker_host, "image", "rm", image_id),
                    check=True,
                    capture_output=True,
                    timeout=60,
                )

            manifest = {
                "format": 2,
                "source_commit": commit,
                "git_tree": git_tree,
                "source_tree_digest_sha256": source,
                "uv_lock_sha256": digest(ROOT / "uv.lock"),
                "dockerfile_sha256": digest(dockerfile),
                "base_image": args.base_image,
                "base_image_reference_digest": args.base_image.split("@", 1)[1],
                "base_image_id": base["Id"],
                "base_image_os": base["Os"],
                "base_image_architecture": base["Architecture"],
                "base_image_repo_digests": base.get("RepoDigests") or [],
                "requested_platform": args.platform,
                "os": expected_os,
                "architecture": expected_arch,
                "build_image_id": image_id,
                "archive_config_digest": archive_meta["config_digest"],
                "imported_image_id": imported_image_id,
                "worker_image_id": imported_image_id,
                "repo_digests": imported.get("RepoDigests") or [],
                "entrypoint": archive_meta["entrypoint"],
                "cmd": archive_meta["cmd"],
                "user": archive_meta["user"],
                "working_dir": archive_meta["working_dir"],
                "runtime_config": archive_meta["runtime_config"],
                "rootfs_diff_ids": archive_meta["diff_ids"],
                "archive_sha256": digest(archive),
            }
            output.mkdir(parents=True)
            shutil.copy2(archive, output / "bc15-worker-image.tar")
            (output / "worker-manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            shutil.copy2(dockerfile, output / "Dockerfile.protected")
            print(json.dumps(manifest, sort_keys=True))
            return 0
    finally:
        subprocess.run(
            docker_command(docker, args.docker_host, "image", "rm", "--force", tag),
            check=False,
            capture_output=True,
            timeout=60,
        )


if __name__ == "__main__":
    raise SystemExit(main())
