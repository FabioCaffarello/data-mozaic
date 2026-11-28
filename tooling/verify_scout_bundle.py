"""Verify bundle structure and source claims against operator-supplied identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path, PurePosixPath

BROKER_SOURCE_PATHS = (
    "apps/scout/pyproject.toml",
    "apps/scout/scout",
    "libs/python/observability/pyproject.toml",
    "libs/python/observability/src/mozaic_observability",
    "libs/python/bot-build/pyproject.toml",
    "libs/python/bot-build/src/mozaic_bot_build",
    "libs/python/contract-resolution/pyproject.toml",
    "libs/python/contract-resolution/src/mozaic_contract_resolution",
    "apps/collector-engineering/pyproject.toml",
    "apps/collector-engineering/collector_engineering",
    "apps/execution-coordination/pyproject.toml",
    "apps/execution-coordination/src/execution_coordination",
    "apps/acquisition-lifecycle/pyproject.toml",
    "apps/acquisition-lifecycle/src/acquisition_lifecycle",
    "apps/collector-engineering/Dockerfile.protected",
    "contracts/payload/draft/v1",
)
BROKER_DEPLOYMENT_FILES = (
    "deployments/systemd/data-mozaic-scout-broker.service",
    "deployments/macos/org.datamozaic.scout-tunnel.plist.example",
    "deployments/sshd/scout-tunnel-match.conf.example",
    "config/scout/scout-tunnel-authorized-key.example",
    "config/scout/broker.env.example",
    "config/scout/reviewer.env.example",
    "config/scout/reviewer-identities.example.json",
    "config/scout/linux-installation.example.json",
    "config/scout/scout-tunnel-ssh-config.example",
    "config/scout/authoring-policy.example.json",
    "config/scout/codex-requirements.example.toml",
)

try:
    from .scout_release_artifact import (
        architecture_for_platform,
        archive_image_config,
        runtime_config_differences,
        sha256_file,
        verify_wheelhouse,
    )
except ImportError:  # direct execution via `python tooling/verify_scout_bundle.py`
    from scout_release_artifact import (
        architecture_for_platform,
        archive_image_config,
        runtime_config_differences,
        sha256_file,
        verify_wheelhouse,
    )


def _fail(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _source_digest(root: Path, *, worker_only: bool = False) -> str:
    files = [root / "uv.lock"]
    if worker_only:
        files.extend(
            [
                root / "apps/collector-engineering/pyproject.toml",
                root / "apps/collector-engineering/Dockerfile.protected",
            ]
        )
        files.extend(
            sorted(
                (root / "apps/collector-engineering/collector_engineering").rglob(
                    "*.py"
                )
            )
        )
    else:
        for relative in BROKER_SOURCE_PATHS:
            path = root / relative
            files.extend(sorted(path.rglob("*") if path.is_dir() else (path,)))
        files.extend(root / relative for relative in BROKER_DEPLOYMENT_FILES)
    digest = hashlib.sha256()
    for path in files:
        if path.is_dir() or path.is_symlink():
            continue
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def _git_identity(root: Path) -> tuple[str, str]:
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        text=True,
    )
    _fail(not status, "source checkout is not clean")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    tree = subprocess.check_output(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True
    ).strip()
    return commit, tree


def verify_bundle(
    root: Path,
    *,
    expected_commit: str,
    expected_tree: str,
    target_platform: str,
    expected_manifest_sha256: str | None = None,
    source_root: Path | None = None,
    docker_host: str | None = None,
) -> dict:
    root = root.resolve(strict=True)
    manifest_path = root / "manifest.json"
    _fail(
        manifest_path.is_file() and not manifest_path.is_symlink(),
        "bundle manifest is missing",
    )
    if expected_manifest_sha256:
        _fail(
            sha256_file(manifest_path) == expected_manifest_sha256,
            "bundle manifest digest differs from the independent expected value",
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _fail(manifest.get("format") == 2, "unsupported bundle manifest format")
    _fail(manifest.get("source_commit") == expected_commit, "source commit mismatch")
    _fail(manifest.get("git_tree") == expected_tree, "Git tree mismatch")
    _fail(
        manifest.get("target_platform") == target_platform, "target platform mismatch"
    )
    expected_os, expected_arch = architecture_for_platform(target_platform)

    listed = manifest.get("files")
    _fail(isinstance(listed, dict) and listed, "bundle file hash list is missing")
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"bundle contains a symlink: {path.relative_to(root)}")
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(root).as_posix())
    expected_files = set(listed)
    missing_files = sorted(expected_files - actual)
    unexpected_files = sorted(actual - expected_files)
    _fail(
        actual == expected_files,
        "bundle file set differs from the manifest; "
        f"missing={missing_files}; unexpected={unexpected_files}",
    )
    for relative, expected in listed.items():
        item = PurePosixPath(relative)
        _fail(
            not item.is_absolute() and ".." not in item.parts,
            "invalid bundle file path",
        )
        path = root.joinpath(*item.parts)
        _fail(
            path.is_file() and not path.is_symlink(),
            f"bundle file is missing: {relative}",
        )
        _fail(sha256_file(path) == expected, f"bundle digest mismatch: {relative}")

    lockfile = root / "uv.lock"
    _fail(
        sha256_file(lockfile) == manifest.get("uv_lock_sha256"), "uv.lock hash mismatch"
    )
    requirements = root / "requirements.txt"
    _fail(
        sha256_file(requirements) == manifest.get("requirements_sha256"),
        "exported requirements hash mismatch",
    )
    _fail(
        "--hash=sha256:" in requirements.read_text(), "requirements are not hash-locked"
    )
    dockerfile = root / "Dockerfile.protected"
    _fail(
        sha256_file(dockerfile) == manifest.get("dockerfile_sha256"),
        "worker Dockerfile hash mismatch",
    )

    worker_path = root / "worker-manifest.json"
    worker = json.loads(worker_path.read_text(encoding="utf-8"))
    _fail(worker.get("format") == 2, "unsupported worker manifest format")
    _fail(
        worker.get("source_commit") == expected_commit, "worker source commit mismatch"
    )
    _fail(worker.get("git_tree") == expected_tree, "worker Git tree mismatch")
    _fail(
        worker.get("requested_platform") == target_platform,
        "worker target platform mismatch",
    )
    _fail(worker.get("os") == expected_os, "worker OS mismatch")
    _fail(worker.get("architecture") == expected_arch, "worker architecture mismatch")
    _fail(
        worker.get("uv_lock_sha256") == manifest["uv_lock_sha256"],
        "worker lock hash mismatch",
    )
    _fail(
        sha256_file(worker_path) == manifest.get("worker_manifest_sha256"),
        "worker manifest hash mismatch",
    )
    archive = root / "bc15-worker-image.tar"
    _fail(
        sha256_file(archive) == worker.get("archive_sha256"),
        "worker archive hash mismatch",
    )
    _fail(
        sha256_file(archive) == manifest.get("worker_archive_sha256"),
        "bundle worker archive hash mismatch",
    )
    image = archive_image_config(archive)
    image_id = worker.get("worker_image_id")
    _fail(image_id == worker.get("build_image_id"), "build image ID mismatch")
    _fail(
        image_id == worker.get("archive_config_digest"),
        "archive config digest mismatch",
    )
    _fail(image_id == worker.get("imported_image_id"), "imported image ID mismatch")
    _fail(
        image_id == image["config_digest"],
        "archive config blob does not match image ID",
    )
    _fail(
        image_id == manifest.get("worker_image_id"), "bundle worker image ID mismatch"
    )
    _fail(
        image_id == manifest.get("worker_archive_config_digest"),
        "bundle archive config digest mismatch",
    )
    _fail(
        image["os"] == expected_os and image["architecture"] == expected_arch,
        "archive platform mismatch",
    )
    for key in ("entrypoint", "cmd", "user", "working_dir"):
        _fail(image[key] == worker.get(key), f"worker {key} mismatch")
    runtime_differences = runtime_config_differences(
        image["runtime_config"], worker.get("runtime_config", {})
    )
    _fail(not runtime_differences, "worker runtime config differs from archive")
    _fail(
        worker.get("dockerfile_sha256") == sha256_file(dockerfile),
        "worker Dockerfile source mismatch",
    )
    _fail(
        worker.get("base_image_reference_digest", "").startswith("sha256:"),
        "worker base image is not pinned by digest",
    )
    _fail(
        worker.get("base_image_id", "").startswith("sha256:")
        and len(worker["base_image_id"]) == 71,
        "worker base image config ID is missing",
    )
    _fail(
        worker.get("base_image_os") == expected_os
        and worker.get("base_image_architecture") == expected_arch,
        "worker base image platform mismatch",
    )
    _fail(
        isinstance(worker.get("base_image_repo_digests"), list),
        "worker base image RepoDigest metadata is invalid",
    )
    _fail(
        worker.get("repo_digests") == manifest.get("repo_digests"),
        "RepoDigest metadata differs",
    )
    verify_wheelhouse(root / "wheelhouse", target_platform)

    if docker_host:
        docker = shutil.which("docker")
        _fail(docker is not None, "Docker CLI is unavailable for imported-image check")
        result = subprocess.run(
            [
                docker,
                "--host",
                docker_host,
                "image",
                "inspect",
                image_id,
                "--format",
                "{{json .}}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        _fail(
            result.returncode == 0,
            "declared worker image is absent from verification daemon",
        )
        imported = json.loads(result.stdout)
        _fail(imported.get("Id") == image_id, "imported worker image ID mismatch")
        _fail(
            imported.get("Os") == expected_os
            and imported.get("Architecture") == expected_arch,
            "imported worker image platform mismatch",
        )
        imported_config = imported.get("Config", {})
        runtime_differences = runtime_config_differences(
            imported_config, worker.get("runtime_config", {})
        )
        _fail(not runtime_differences, "imported worker runtime config mismatch")
        for key, field in (
            ("Entrypoint", "entrypoint"),
            ("Cmd", "cmd"),
            ("User", "user"),
            ("WorkingDir", "working_dir"),
        ):
            _fail(
                imported_config.get(key) == worker.get(field),
                f"imported image {field} mismatch",
            )
        base_result = subprocess.run(
            [
                docker,
                "--host",
                docker_host,
                "image",
                "inspect",
                worker["base_image"],
                "--format",
                "{{json .}}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
        _fail(
            base_result.returncode == 0,
            "pinned worker base image is absent from verification daemon",
        )
        base = json.loads(base_result.stdout)
        _fail(
            base.get("Id") == worker["base_image_id"],
            "pinned worker base image config ID mismatch",
        )
        _fail(
            base.get("Os") == expected_os and base.get("Architecture") == expected_arch,
            "pinned worker base image platform mismatch",
        )
        _fail(
            (base.get("RepoDigests") or []) == worker["base_image_repo_digests"],
            "pinned worker base image RepoDigest metadata mismatch",
        )

    if source_root is not None:
        source_root = source_root.resolve(strict=True)
        commit, tree = _git_identity(source_root)
        _fail(
            commit == expected_commit and tree == expected_tree,
            "source checkout identity mismatch",
        )
        _fail(
            _source_digest(source_root) == manifest.get("source_tree_digest_sha256"),
            "broker source digest mismatch",
        )
        _fail(
            _source_digest(source_root, worker_only=True)
            == worker.get("source_tree_digest_sha256"),
            "worker source digest mismatch",
        )
        _fail(
            sha256_file(source_root / "uv.lock") == manifest["uv_lock_sha256"],
            "bundle lockfile does not match source checkout",
        )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--target-platform", required=True)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument(
        "--docker-host", help="inspect image in an operator-selected daemon"
    )
    args = parser.parse_args()
    try:
        manifest = verify_bundle(
            args.bundle,
            expected_commit=args.expected_commit,
            expected_tree=args.expected_tree,
            target_platform=args.target_platform,
            expected_manifest_sha256=args.expected_manifest_sha256,
            source_root=args.source_root,
            docker_host=args.docker_host,
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"release bundle rejected: {error}") from error
    print(
        json.dumps(
            {
                "verified_files": len(manifest["files"]),
                "source_commit": manifest["source_commit"],
                "git_tree": manifest["git_tree"],
                "target_platform": manifest["target_platform"],
                "worker_image_id": manifest["worker_image_id"],
                "manifest_sha256": sha256_file(args.bundle / "manifest.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
