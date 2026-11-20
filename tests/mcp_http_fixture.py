"""Synthetic Streamable HTTP broker fixture used only by deterministic E2E tests."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from scout.adapters.fixtures import FixtureExplorer
from scout.adapters.local_custody import LocalFileEvidenceCustody
from scout.application import ScoutApplication
from scout.domain import AuthorizationError, PolicyDecisionRef, ScoutScope, SourceRef
from scout.fakes import FakePolicy, FakeSessionRepository, FakeSourceLookup
from scout.tool_host.http_transport import (
    HTTPBrokerSettings,
    create_streamable_http_app,
)
from scout.tool_host.protected_replay import DockerProtectedReplayExecutor
from scout.tool_host.server import create_server


class _TestSourceLookup(FakeSourceLookup):
    def resolve(self, source):
        if source not in self.sources:
            raise AuthorizationError("source or revision is unavailable")
        return super().resolve(source)


def main() -> None:
    source = SourceRef("catalog-test", "revision-1")
    scope = ScoutScope(("https://catalog.test",), ("/",), ("GET",), 4, 60)
    decision = PolicyDecisionRef(
        "approval-local",
        "local-analyst",
        source,
        True,
        datetime.now(UTC) + timedelta(hours=1),
        scope,
    )
    app = ScoutApplication(
        _TestSourceLookup({source: scope}),
        FakePolicy(decision),
        FakeSessionRepository(),
        LocalFileEvidenceCustody(Path(os.environ["SCOUT_TEST_CUSTODY_ROOT"])),
        FixtureExplorer(),
    )
    oracle_path = (
        Path(__file__).parent / "fixtures/scout-authoring/mcp-fixture-acceptance.json"
    )
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    source_key = (oracle["source"]["source_id"], oracle["source"]["revision"])
    server = create_server(
        app,
        "local-analyst",
        protected_replay_executor=DockerProtectedReplayExecutor(
            os.environ["SCOUT_TEST_WORKER_IMAGE_DIGEST"],
            docker_executable=os.environ.get(
                "SCOUT_TEST_DOCKER_EXECUTABLE", shutil.which("docker") or ""
            ),
            registry_path=Path(os.environ["SCOUT_TEST_REGISTRY_PATH"]),
            allow_unprivileged_test_cli=True,
        ),
        acceptance_expectations={source_key: oracle},
    )
    token = os.environ["SCOUT_TEST_BROKER_TOKEN"]
    settings = HTTPBrokerSettings(token)
    uvicorn.run(
        create_streamable_http_app(server, settings),
        host="127.0.0.1",
        port=8765,
        lifespan="on",
        access_log=False,
        log_level="critical",
    )


if __name__ == "__main__":
    main()
