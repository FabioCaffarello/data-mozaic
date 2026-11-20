"""Entrypoint for the operator-managed Scout Streamable HTTP service."""

from __future__ import annotations

import logging

import uvicorn

from .composition import build_trusted_http_server
from .http_transport import create_streamable_http_app, settings_from_environment


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = settings_from_environment()
    server = build_trusted_http_server()
    app = create_streamable_http_app(server, settings)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8765,
        lifespan="on",
        access_log=False,
        log_level="info",
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
