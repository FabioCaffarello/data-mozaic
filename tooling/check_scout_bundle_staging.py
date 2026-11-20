"""Smoke-test an unprivileged Linux broker bundle installed outside the checkout."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
BROKER_URL = "http://127.0.0.1:8765/mcp"


def _assert_imports_are_installed(python: Path) -> None:
    code = (
        "import pathlib,sys,mcp,scout,scout.tool_host.__main__; "
        "prefix=pathlib.Path(sys.prefix).resolve(); "
        "assert pathlib.Path(mcp.__file__).resolve().is_relative_to(prefix); "
        "assert pathlib.Path(scout.__file__).resolve().is_relative_to(prefix); "
        "print(mcp.__file__); print(scout.__file__)"
    )
    result = subprocess.run(
        [str(python), "-I", "-c", code],
        cwd="/",
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("installed bundle imports failed")
    if "/site-packages/" not in result.stdout:
        raise RuntimeError("installed modules did not resolve from the venv")
    if str(ROOT) in result.stdout:
        raise RuntimeError("installed runtime imported from the checkout")
    subprocess.run(
        ["uv", "pip", "check", "--python", str(python)],
        cwd="/",
        check=True,
        timeout=30,
        stdout=subprocess.DEVNULL,
    )


async def _exercise_http(token: str) -> None:
    try:
        await asyncio.to_thread(
            _request_unauthenticated,
            Request(BROKER_URL, headers={"Host": "127.0.0.1:8765"}),
        )
        raise RuntimeError("unauthenticated MCP request unexpectedly succeeded")
    except HTTPError as error:
        if error.code != 401:
            raise RuntimeError(
                "unauthenticated MCP request returned an unexpected status"
            )
    client = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    async with (
        client,
        streamable_http_client(BROKER_URL, http_client=client) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
    names = {tool.name for tool in result.tools}
    required = {"scout_preflight", "scout_bc15_replay", "scout_bc15_accept"}
    if not required.issubset(names):
        raise RuntimeError("installed broker MCP tool catalog is incomplete")


def _request_unauthenticated(request: Request) -> None:
    with urlopen(request, timeout=3):
        return


def main() -> int:
    if os.name != "posix" or not os.environ.get("SCOUT_DATABASE_URL"):
        raise SystemExit(
            "Linux staging smoke test requires the synthetic CI PostgreSQL DSN"
        )
    staging = Path(os.environ["SCOUT_STAGING_PREFIX"]).resolve(strict=True)
    python = staging / "venv/bin/python"
    if not python.is_file():
        raise SystemExit("staged broker Python environment is missing")
    _assert_imports_are_installed(python)
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker CLI is unavailable")
    docker = str(Path(docker).resolve(strict=True))
    worker_image = os.environ["SCOUT_BC15_WORKER_IMAGE_DIGEST"]

    with tempfile.TemporaryDirectory(prefix="scout-broker-staging-") as temporary:
        root = Path(temporary)
        policy = root / "policy.json"
        policy.write_text(json.dumps({"version": 1, "sources": [], "approvals": []}))
        policy.chmod(0o600)
        state = root / "state"
        state.mkdir(mode=0o700)
        evidence = root / "evidence"
        evidence.mkdir(mode=0o700)
        artifacts = root / "artifacts"
        artifacts.mkdir(mode=0o700)
        token = "ci-synthetic-" + secrets.token_hex(32)
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(root),
            "SCOUT_TRUSTED_HTTP": "1",
            "SCOUT_LOCAL_PRINCIPAL": "release-ci-synthetic",
            "SCOUT_DATABASE_URL": os.environ["SCOUT_DATABASE_URL"],
            "SCOUT_HOST_POLICY_FILE": str(policy),
            "SCOUT_RUNTIME_STATE_DIR": str(state),
            "SCOUT_EVIDENCE_ROOT": str(evidence),
            "SCOUT_ARTIFACT_ROOT": str(artifacts),
            "SCOUT_BC15_WORKER_IMAGE_DIGEST": worker_image,
            "SCOUT_DOCKER_EXECUTABLE": docker,
            "SCOUT_DOCKER_CONTEXT": os.environ.get("SCOUT_DOCKER_CONTEXT", "default"),
            "SCOUT_DOCKER_ENDPOINT": os.environ.get(
                "SCOUT_DOCKER_ENDPOINT", "unix:///var/run/docker.sock"
            ),
            "SCOUT_BROKER_TOKEN": token,
        }
        process = subprocess.Popen(
            [str(python), "-I", "-m", "scout.tool_host"],
            cwd="/",
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("staged broker exited during startup")
                try:
                    request = Request(
                        "http://127.0.0.1:8765/healthz",
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Host": "127.0.0.1:8765",
                        },
                    )
                    with urlopen(request, timeout=1) as response:
                        if response.status == 200:
                            break
                except (OSError, HTTPError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("staged broker health check did not become ready")
            asyncio.run(_exercise_http(token))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    print(
        "Linux staged broker imports, startup, authentication, and MCP discovery passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
