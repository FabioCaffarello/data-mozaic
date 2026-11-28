"""Launch the fixed, sandboxed Compose worker for one BC04-authorized attempt."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[5]
_COMPOSE = _ROOT / "deployments/compose/acquisition-live.compose.yaml"
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class DockerComposeProtectedLiveWorker:
    """Fixed Compose service launcher; caller cannot select a command or image."""

    def __init__(self, *, compose_project: str, repository_root: Path = _ROOT) -> None:
        if not _PROJECT.fullmatch(compose_project):
            raise ValueError("MOZAIC_LIVE_COMPOSE_PROJECT is invalid")
        root = repository_root.resolve(strict=True)
        if root != _ROOT.resolve(strict=True):
            raise ValueError("operator worker must use the principal repository")
        self._root = root
        self._project = compose_project
        self._result_owners: dict[str, tempfile.TemporaryDirectory[str]] = {}

    def execute(
        self,
        *,
        submission: object,
        run: object,
        lease: object,
        capability: Mapping[str, object],
    ) -> Path:
        binding = run.intent.binding
        expected_runtime = binding.runtime_image_digest
        image = f"{self._project}-operator-worker-scrapy-live:latest"
        inspect = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            cwd=self._root,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        actual_runtime = inspect.stdout.strip()
        if inspect.returncode or actual_runtime != expected_runtime:
            raise RuntimeError(
                "protected worker image is unavailable or differs from the approved runtime; build the registered BotBuild runtime first"
            )

        canonical_parameters = submission.intent.parameters_json
        if hashlib.sha256(
            canonical_parameters
        ).hexdigest() != binding.input_parameters_digest.removeprefix("sha256:"):
            raise ValueError("Submission parameters differ from the BC04 binding")
        owner = tempfile.TemporaryDirectory(prefix="mozaic-operator-attempt-")
        try:
            private = Path(owner.name)
            os.chmod(private, 0o700)
            capability_path = private / "capability"
            encoded = base64.urlsafe_b64encode(
                json.dumps(capability, separators=(",", ":")).encode("utf-8")
            ).rstrip(b"=")
            capability_path.write_bytes(encoded)
            capability_path.chmod(0o444)
            parameters_path = private / "parameters.json"
            parameters_path.write_bytes(canonical_parameters)
            parameters_path.chmod(0o444)
            result_parent = private / "result-parent"
            result_parent.mkdir(mode=0o777)
            result_parent.chmod(0o777)

            env = os.environ.copy()
            env.update(
                {
                    "MOZAIC_LIVE_CAPABILITY_FILE": str(capability_path),
                    "MOZAIC_LIVE_INPUT_PARAMETERS_FILE": str(parameters_path),
                    "MOZAIC_ENABLE_INPUT_PARAMETERS": "/run/mozaic/input-parameters",
                    "MOZAIC_LIVE_RESULT_PARENT": str(result_parent),
                }
            )
            command = [
                "docker",
                "compose",
                "--project-name",
                self._project,
                "--file",
                str(_COMPOSE),
                "--profile",
                "operator",
                "run",
                "--rm",
                "--no-TTY",
                "operator-worker-scrapy-live",
            ]
            timeout = min(360, max(90, int(binding.max_duration_seconds) + 60))
            try:
                result = subprocess.run(
                    command,
                    cwd=self._root,
                    env=env,
                    check=False,
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    "protected worker exceeded its bounded timeout"
                ) from error
            if result.returncode:
                diagnostic = ""
                for line in reversed(
                    (result.stdout + b"\n" + result.stderr)
                    .decode("utf-8", errors="replace")
                    .splitlines()
                ):
                    marker = line.find("{")
                    if marker < 0:
                        continue
                    try:
                        status = json.loads(line[marker:])
                    except json.JSONDecodeError:
                        continue
                    value = (
                        status.get("diagnostic") if isinstance(status, dict) else None
                    )
                    if (
                        isinstance(status, dict)
                        and status.get("state") == "failed"
                        and isinstance(value, str)
                        and len(value) <= 256
                    ):
                        diagnostic = value
                        break
                if not diagnostic:
                    compose_detail = result.stderr.decode("utf-8", errors="replace")[
                        -1200:
                    ]
                    for name, secret in env.items():
                        if (
                            secret
                            and any(
                                marker in name.upper()
                                for marker in (
                                    "TOKEN",
                                    "KEY",
                                    "PASSWORD",
                                    "DATABASE_URL",
                                )
                            )
                            and len(secret) >= 8
                        ):
                            compose_detail = compose_detail.replace(
                                secret, "[redacted]"
                            )
                    diagnostic = compose_detail.strip()
                suffix = f": {diagnostic}" if diagnostic else ""
                raise RuntimeError(
                    "protected worker reported a failed execution" + suffix
                )
            result_dir = result_parent / "result"
            if result_dir.is_symlink() or not result_dir.is_dir():
                raise RuntimeError("protected worker did not produce a regular result")
            self._result_owners[str(result_dir)] = owner
            return result_dir
        except BaseException:
            owner.cleanup()
            raise

    def cleanup(self, result: object) -> None:
        if not isinstance(result, Path):
            return
        owner = self._result_owners.pop(str(result), None)
        if owner is not None:
            owner.cleanup()
