"""Run the local Scout MCP tool host over STDIO only."""

import logging

from .composition import build_server
from .server import serve


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=__import__("sys").stderr)
    serve(build_server())


if __name__ == "__main__":
    main()
