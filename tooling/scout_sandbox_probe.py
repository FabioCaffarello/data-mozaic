"""Docker-only proof that a worker can reach only the explicitly configured proxy."""

import socket
import sys
import time


def main() -> int:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=1.5):
            print("unexpected direct egress connection succeeded", file=sys.stderr)
            return 1
    except OSError:
        pass
    try:
        socket.getaddrinfo("example.com", 443)
    except OSError:
        pass
    else:
        print("unexpected direct DNS lookup succeeded", file=sys.stderr)
        return 1

    proxy = None
    last_error = None
    for _ in range(20):
        try:
            proxy = socket.create_connection(("squid", 3128), timeout=1)
            break
        except OSError as error:
            last_error = error
            time.sleep(0.25)
    if proxy is None:
        print(f"isolated proxy was not reachable: {last_error}", file=sys.stderr)
        return 1

    def connect_response(host: str) -> bytes:
        with socket.create_connection(("squid", 3128), timeout=2) as connection:
            connection.sendall(
                f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode()
            )
            return connection.recv(2048)

    for host, reason in (
        ("blocked-target.example.test", "private-address destination"),
        ("not-approved.example.test", "unregistered source hostname"),
    ):
        try:
            response = connect_response(host)
        except OSError as error:
            print(f"proxy request failed for {host}: {error}", file=sys.stderr)
            return 1
        if b"403" not in response.split(b"\r\n", 1)[0]:
            print(f"proxy did not block {reason}: {response!r}", file=sys.stderr)
            return 1
    print(
        "direct TCP/DNS egress blocked; proxy denied private IP and unregistered source"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
