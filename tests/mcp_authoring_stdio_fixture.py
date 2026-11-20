"""Test-only STDIO harness for the legacy PostgreSQL/HTTPS integration suite.

This module is not a service entrypoint and is not used by the project Codex
configuration. Protected replay acceptance has its own Streamable HTTP E2E.
"""

from scout.tool_host.composition import build_server
from scout.tool_host.server import serve

if __name__ == "__main__":
    serve(build_server())
