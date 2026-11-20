"""Build a disposable test worker before launching MCP; service calls never build."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    docker = shutil.which("docker")
    uv = shutil.which("uv")
    if not docker or not uv:
        raise SystemExit("Docker and uv are required for the protected replay check")
    tag = f"data-mozaic-bc15-test:{uuid.uuid4().hex[:16]}"
    base_tag = f"data-mozaic-python-test:{uuid.uuid4().hex[:16]}"
    try:
        base_image = subprocess.run(
            [docker, "image", "inspect", "python:3.12-slim", "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if base_image.returncode:
            subprocess.run([docker, "pull", "python:3.12-slim"], check=True)
            base_image = subprocess.run(
                [docker, "image", "inspect", "python:3.12-slim", "--format", "{{.Id}}"],
                capture_output=True,
                text=True,
                check=True,
            )
        base_id = base_image.stdout.strip()
        if not base_id.startswith("sha256:") or len(base_id) != 71:
            raise SystemExit(
                "test Python base image did not resolve to an immutable ID"
            )
        # Docker build accepts a name/tag or registry digest as FROM, but not a
        # local image config ID. Give the already-resolved local image a unique
        # disposable tag so the build cannot silently pull a mutable tag.
        subprocess.run([docker, "image", "tag", base_id, base_tag], check=True)
        with tempfile.TemporaryDirectory(prefix="scout-bc15-test-image-") as tmp:
            context = Path(tmp)
            subprocess.run(
                [
                    uv,
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
                    "--quiet",
                    "--build-arg",
                    f"PYTHON_BASE_IMAGE={base_tag}",
                    "--tag",
                    tag,
                    "--file",
                    str(context / "Dockerfile.protected"),
                    str(context),
                ],
                cwd=ROOT,
                check=True,
                timeout=300,
            )
        image = subprocess.check_output(
            [docker, "image", "inspect", tag, "--format", "{{.Id}}"], text=True
        ).strip()
        if not image.startswith("sha256:") or len(image) != 71:
            raise SystemExit(
                "test worker image did not resolve to an immutable image ID"
            )
        env = os.environ.copy()
        env["SCOUT_PROTECTED_REPLAY_E2E"] = "1"
        env["SCOUT_TEST_WORKER_IMAGE_DIGEST"] = image
        return subprocess.run(
            [
                uv,
                "run",
                "--locked",
                "python",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests/e2e",
                "-p",
                "test_scout_protected_replay.py",
                "-v",
            ],
            cwd=ROOT,
            env=env,
            check=False,
        ).returncode
    finally:
        if docker:
            subprocess.run(
                [docker, "image", "rm", "--force", tag],
                check=False,
                capture_output=True,
            )
            subprocess.run(
                [docker, "image", "rm", "--force", base_tag],
                check=False,
                capture_output=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
