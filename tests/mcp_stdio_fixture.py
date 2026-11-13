"""Subprocess composition for the real MCP STDIO protocol test only."""

import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scout.adapters.fixtures import FixtureExplorer
from scout.adapters.local_custody import LocalFileEvidenceCustody
from scout.application import ScoutApplication
from scout.domain import AuthorizationError, PolicyDecisionRef, ScoutScope, SourceRef
from scout.fakes import (
    FakePolicy,
    FakeSessionRepository,
    FakeSourceLookup,
)
from scout.tool_host.server import create_server, serve


class _TestSourceLookup(FakeSourceLookup):
    def resolve(self, source):
        if source not in self.sources:
            raise AuthorizationError("source or revision is unavailable")
        return super().resolve(source)


def main():
    def deny_socket(*_args, **_kwargs):
        raise AssertionError("hermetic MCP fixture attempted a network socket")

    socket.socket.connect = deny_socket
    socket.create_connection = deny_socket
    socket.getaddrinfo = deny_socket
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
    serve(create_server(app, "local-analyst"))


if __name__ == "__main__":
    main()
