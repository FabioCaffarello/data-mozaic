"""Docker-backed BC15 adapter used only by the trusted local MCP composition."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISDIR, S_ISREG, S_ISVTX

from .protected_run_registry import ProtectedRunRegistry

MAX_REQUEST_BYTES = 48_000_000
MAX_DRAFT_BYTES = 64_000
MAX_PAGES = 20
MAX_TOTAL_BODY_BYTES = 24_000_000
MAX_BODY_BYTES = 2_000_000
MAX_RUNTIME_SECONDS = 60
_LOG = logging.getLogger(__name__)


class ProtectedReplayError(ValueError):
    """A typed, bounded failure from the protected executor."""


class DockerProtectedReplayExecutor:
    """Run BC15 in a private container volume without host path mounts."""

    def __init__(
        self,
        worker_image_digest: str,
        docker_executable: str,
        registry_path: Path | None = None,
        docker_context: str = "default",
        docker_endpoint: str = "unix:///var/run/docker.sock",
        allow_unprivileged_test_cli: bool = False,
    ):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", worker_image_digest):
            raise ProtectedReplayError("protected BC15 image digest is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", docker_context):
            raise ProtectedReplayError("protected Docker context is invalid")
        if not docker_endpoint.startswith("unix://"):
            raise ProtectedReplayError(
                "protected Docker endpoint must be a local socket"
            )
        self._docker = self._validate_docker_executable(
            docker_executable, allow_unprivileged_test_cli=allow_unprivileged_test_cli
        )
        self._context = docker_context
        self._endpoint = docker_endpoint
        self._image = worker_image_digest
        self._verify_runtime()
        self._lock = threading.Lock()
        self._volume_lock = threading.Lock()
        self._active = 0
        if registry_path is None:
            registry_path = (
                Path.home() / ".local/state/data-mozaic/scout-bc15/bc15-runs.sqlite3"
            )
        self._registry = ProtectedRunRegistry(registry_path)
        self._reconcile_runs()
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, name="bc15-volume-retention", daemon=True
        )
        self._cleanup_thread.start()

    @staticmethod
    def _validate_docker_executable(
        configured_path: str, *, allow_unprivileged_test_cli: bool = False
    ) -> str:
        path = Path(configured_path)
        if not path.is_absolute():
            raise ProtectedReplayError("Docker executable must be an absolute path")
        try:
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as error:
            raise ProtectedReplayError(
                "configured Docker executable is unavailable"
            ) from error
        owner_is_trusted = metadata.st_uid == 0 or (
            allow_unprivileged_test_cli and metadata.st_uid == os.geteuid()
        )
        if (
            not S_ISREG(metadata.st_mode)
            or not owner_is_trusted
            or metadata.st_mode & 0o022
            or metadata.st_mode & 0o6000
            or not os.access(resolved, os.X_OK)
        ):
            raise ProtectedReplayError(
                "Docker executable must be trusted-owned, executable, and non-writable"
            )
        for parent in resolved.parents:
            try:
                metadata = parent.stat()
            except OSError as error:
                raise ProtectedReplayError(
                    "Docker executable parent directory is unavailable"
                ) from error
            parent_is_trusted = metadata.st_uid == 0 or (
                allow_unprivileged_test_cli and metadata.st_uid == os.geteuid()
            )
            private_temp_parent = (
                allow_unprivileged_test_cli
                and metadata.st_uid == 0
                and metadata.st_mode & S_ISVTX
            )
            developer_owned_parent = (
                allow_unprivileged_test_cli
                and metadata.st_uid == os.geteuid()
                and not metadata.st_mode & 0o002
            )
            if (
                not S_ISDIR(metadata.st_mode)
                or not parent_is_trusted
                or (
                    metadata.st_mode & 0o022
                    and not private_temp_parent
                    and not developer_owned_parent
                )
            ):
                raise ProtectedReplayError(
                    "Docker executable path contains an untrusted directory"
                )
        return str(resolved)

    def _run(
        self, arguments: list[str], *, input_bytes: bytes | None = None, timeout=30
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self._docker, *arguments],
                cwd="/",
                input=input_bytes,
                capture_output=True,
                timeout=timeout,
                check=False,
                env={
                    "PATH": "/usr/bin:/bin",
                    **{
                        key: os.environ[key]
                        for key in ("HOME", "DOCKER_CONFIG")
                        if key in os.environ
                    },
                },
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProtectedReplayError(
                "protected BC15 executor operation failed"
            ) from error

    def _verify_runtime(self) -> None:
        endpoint = self._run(
            [
                "context",
                "inspect",
                self._context,
                "--format",
                "{{.Endpoints.docker.Host}}",
            ]
        )
        host = endpoint.stdout.decode("utf-8", errors="replace").strip()
        if endpoint.returncode or host != self._endpoint:
            raise ProtectedReplayError(
                "protected BC15 Docker context does not match configured endpoint"
            )
        image = self._run(
            self._docker_args("image", "inspect", self._image, "--format", "{{.Id}}")
        )
        if (
            image.returncode
            or image.stdout.decode("ascii", errors="ignore").strip() != self._image
        ):
            raise ProtectedReplayError("prebuilt protected BC15 image is unavailable")

    def _docker_args(self, *args: str) -> list[str]:
        return ["--context", self._context, *args]

    @staticmethod
    def _worker_args(volume: str, image_id: str) -> list[str]:
        if not re.fullmatch(r"dm-bc15-[a-f0-9]{32}", volume) or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", image_id
        ):
            raise ProtectedReplayError("protected BC15 runtime reference is invalid")
        return [
            "run",
            "--rm",
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=32",
            "--memory=256m",
            "--cpus=0.5",
            "--user=65532:65532",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
            "--mount",
            f"type=volume,source={volume},target=/results",
            image_id,
        ]

    def _reconcile_runs(self) -> None:
        listed = self._run(
            self._docker_args(
                "volume", "ls", "--quiet", "--filter", "label=data-mozaic.owner=bc15"
            )
        )
        if listed.returncode:
            raise ProtectedReplayError("protected BC15 volume inventory is unavailable")
        known = {item["volume"]: item for item in self._registry.entries()}
        present = set()
        for volume in listed.stdout.decode("ascii", errors="ignore").splitlines():
            if not re.fullmatch(r"dm-bc15-[a-f0-9]{32}", volume):
                continue
            inspected = self._run(
                self._docker_args(
                    "volume",
                    "inspect",
                    volume,
                    "--format",
                    "{{.Labels.data-mozaic.run_id}} {{.Labels.data-mozaic.owner}}",
                )
            )
            if inspected.returncode:
                continue
            run_id = volume.removeprefix("dm-bc15-")
            labels = inspected.stdout.decode("ascii", errors="ignore").strip().split()
            if labels != [run_id, "bc15"]:
                continue
            present.add(volume)
            entry = known.get(volume)
            if entry is None:
                self._registry.create(run_id, volume, "orphan", "orphan")
                self._registry.update(run_id, "incomplete")
            elif entry["state"] in {"creating", "running"}:
                self._registry.update(run_id, "incomplete")
        for volume, entry in known.items():
            if volume not in present and entry["state"] in {"creating", "running"}:
                self._registry.update(entry["run_id"], "incomplete")
        self._expire_runs()

    def _remove_owned_volume(self, entry: dict) -> bool:
        volume = entry["volume"]
        if not re.fullmatch(r"dm-bc15-[a-f0-9]{32}", volume):
            return False
        inspect = self._run(
            self._docker_args(
                "volume",
                "inspect",
                volume,
                "--format",
                "{{.Name}} {{.Labels.data-mozaic.run_id}} {{.Labels.data-mozaic.owner}}",
            )
        )
        if inspect.returncode:
            return True
        expected = f"{volume} {entry['run_id']} bc15"
        if inspect.stdout.decode("ascii", errors="ignore").strip() != expected:
            return False
        return self._run(self._docker_args("volume", "rm", volume)).returncode == 0

    def _expire_runs(self) -> None:
        now = datetime.now(UTC)
        for entry in self._registry.entries():
            if entry["state"] == "expired":
                continue
            if datetime.fromisoformat(entry["expires_at"]) > now:
                continue
            if self._remove_owned_volume(entry):
                self._registry.update(entry["run_id"], "expired")

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(60):
            try:
                self._expire_runs()
            except Exception as error:  # noqa: BLE001 - GC must not kill the broker
                _LOG.warning(
                    "protected BC15 retention pass failed: %s", type(error).__name__
                )
                continue

    def _ensure_capacity(self) -> None:
        listed = self._run(
            self._docker_args(
                "volume", "ls", "--quiet", "--filter", "label=data-mozaic.owner=bc15"
            )
        )
        if listed.returncode:
            raise ProtectedReplayError("protected BC15 volume inventory is unavailable")
        owned = [
            item
            for item in listed.stdout.decode("ascii", errors="ignore").splitlines()
            if re.fullmatch(r"dm-bc15-[a-f0-9]{32}", item)
        ]
        if len(owned) >= 100:
            raise ProtectedReplayError(
                "protected BC15 volume retention capacity is full"
            )

    def get_run(self, run_id: str) -> dict | None:
        self._expire_runs()
        return self._registry.get(run_id)

    def claim_approval_once(
        self, principal: str, source_id: str, revision: str, expires_at: str
    ) -> None:
        self._registry.claim_approval_once(principal, source_id, revision, expires_at)

    def execute(
        self,
        *,
        action: str,
        session_id: str,
        principal: str,
        draft_yaml: str,
        transcript: dict,
        pages,
        expectations: dict,
    ) -> tuple[str, dict]:
        if action not in {"replay", "accept"}:
            raise ProtectedReplayError("requested BC15 action is unsupported")
        if (
            not isinstance(draft_yaml, str)
            or len(draft_yaml.encode("utf-8")) > MAX_DRAFT_BYTES
        ):
            raise ProtectedReplayError("playbook draft exceeds the size limit")
        if not 1 <= len(pages) <= MAX_PAGES:
            raise ProtectedReplayError("fixture page count exceeds the limit")
        encoded_pages = []
        total = 0
        for page in pages:
            body = page.body
            total += len(body)
            if (
                len(body) > MAX_BODY_BYTES
                or len(body) != page.size_bytes
                or hashlib.sha256(body).hexdigest() != page.sha256
            ):
                raise ProtectedReplayError("fixture custody bytes failed verification")
            encoded_pages.append(
                {
                    "url": page.url,
                    "status": page.status,
                    "content_type": page.content_type,
                    "evidence_ref": page.evidence_ref,
                    "artifact_id": page.artifact_id,
                    "sha256": page.sha256,
                    "size_bytes": page.size_bytes,
                    "body_base64": base64.b64encode(body).decode("ascii"),
                    "requested_url": page.requested_url,
                    "sequence": page.sequence,
                    "request_count": page.request_count,
                }
            )
        if total > MAX_TOTAL_BODY_BYTES:
            raise ProtectedReplayError("fixture bytes exceed the aggregate limit")
        run_id = uuid.uuid4().hex
        request = {
            "action": action,
            "run_id": run_id,
            "draft_yaml": draft_yaml,
            "transcript": transcript,
            "fixtures": encoded_pages,
            "expectations": expectations,
        }
        payload = json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(payload) > MAX_REQUEST_BYTES:
            raise ProtectedReplayError("executor request exceeds the size limit")
        volume = "dm-bc15-" + run_id
        with self._lock:
            if self._active >= 2:
                raise ProtectedReplayError("protected BC15 executor is busy")
            self._active += 1
        try:
            with self._volume_lock:
                self._expire_runs()
                self._ensure_capacity()
                self._registry.create(run_id, volume, session_id, principal)
                created = self._run(
                    self._docker_args(
                        "volume",
                        "create",
                        "--label",
                        "data-mozaic.owner=bc15",
                        "--label",
                        f"data-mozaic.run_id={run_id}",
                        volume,
                    )
                )
                if created.returncode or created.stdout.decode().strip() != volume:
                    self._registry.update(run_id, "incomplete")
                    raise ProtectedReplayError(
                        "protected BC15 output storage is unavailable"
                    )
            try:
                result = self._run(
                    self._docker_args(*self._worker_args(volume, self._image)),
                    input_bytes=payload,
                    timeout=MAX_RUNTIME_SECONDS,
                )
                self._registry.update(run_id, "running")
                if result.returncode or len(result.stdout) > 1_000_000:
                    raise ProtectedReplayError("protected BC15 replay did not complete")
                response = json.loads(result.stdout)
                if response.get("state") != "passed" or not isinstance(
                    response.get("report"), dict
                ):
                    raise ProtectedReplayError(
                        "protected BC15 replay or acceptance failed"
                    )
                if len(json.dumps(response["report"]).encode("utf-8")) > 256_000:
                    raise ProtectedReplayError(
                        "protected BC15 report exceeds the output limit"
                    )
                self._registry.update(run_id, "passed", response["report"])
                return run_id, response["report"]
            except Exception:
                self._registry.update(run_id, "incomplete")
                raise
        finally:
            with self._lock:
                self._active -= 1
