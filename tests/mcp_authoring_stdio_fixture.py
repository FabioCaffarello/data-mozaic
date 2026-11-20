"""Test-only STDIO harness for the legacy PostgreSQL/HTTPS integration suite.

This module is not a service entrypoint and is not used by the project Codex
configuration. Protected replay acceptance has its own Streamable HTTP E2E.
"""

import os
import shutil
from pathlib import Path

from scout.tool_host.composition import build_server
from scout.tool_host.protected_replay import DockerProtectedReplayExecutor
from scout.tool_host.server import serve

if __name__ == "__main__":
    executor = DockerProtectedReplayExecutor(
        os.environ["SCOUT_TEST_WORKER_IMAGE_DIGEST"],
        docker_executable=os.environ.get(
            "SCOUT_TEST_DOCKER_EXECUTABLE", shutil.which("docker") or ""
        ),
        registry_path=Path(os.environ["SCOUT_TEST_REGISTRY_PATH"]),
        allow_unprivileged_test_cli=True,
    )
    serve(build_server(protected_replay_executor=executor))
