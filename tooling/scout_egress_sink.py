"""Inert TCP sink proving whether Squid tried an explicitly forbidden target."""

from __future__ import annotations

import argparse
import json
import socketserver
import time


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        print(
            json.dumps(
                {
                    "event": "egress-sink.accept",
                    "peer": self.request.getpeername(),
                    "time_ns": time.time_ns(),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        self.request.settimeout(0.25)
        try:
            self.request.recv(1024)
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=443)
    args = parser.parse_args()
    Server(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
