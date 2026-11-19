"""Hermetic programmable DNS responder for the real Squid E2E boundary."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _read_name(packet: bytes, offset: int) -> tuple[str, int]:
    labels = []
    while True:
        length = packet[offset]
        if length == 0:
            return ".".join(labels).lower(), offset + 1
        if length & 0xC0:
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            name, _ = _read_name(packet, pointer)
            return name, offset + 2
        offset += 1
        labels.append(packet[offset : offset + length].decode("ascii"))
        offset += length


def _encode_name(name: str) -> bytes:
    if not name:
        return b"\0"
    return (
        b"".join(
            bytes((len(label),)) + label.encode("ascii") for label in name.split(".")
        )
        + b"\0"
    )


class DNSState:
    def __init__(self):
        self.lock = threading.Lock()
        self.zones: dict[str, dict] = {}
        self.queries: list[dict[str, str]] = []

    def configure(self, zones: dict[str, dict], *, reset_queries: bool = True) -> None:
        with self.lock:
            self.zones = {
                name.rstrip(".").lower(): value for name, value in zones.items()
            }
            if reset_queries:
                self.queries.clear()

    def resolve(
        self, name: str, qtype: int
    ) -> tuple[list[tuple[int, bytes]], int, int]:
        qtype_name = {1: "A", 28: "AAAA"}.get(qtype, str(qtype))
        with self.lock:
            self.queries.append({"name": name, "type": qtype_name})
            zone = self.zones.get(name)
            if zone is None:
                return [], 3, 0
            if zone.get("drop"):
                return [], -1, 0
            ttl = max(0, min(int(zone.get("ttl", 30)), 300))
            rcode = int(zone.get("rcode", 0))
            answers = []
            cname = zone.get("CNAME")
            if cname:
                target = cname.rstrip(".").lower()
                answers.append((5, _encode_name(target)))
                target_zone = self.zones.get(target, {})
                for value in target_zone.get(qtype_name, []):
                    address = ipaddress.ip_address(value)
                    wanted_version = 4 if qtype == 1 else 6 if qtype == 28 else None
                    if address.version == wanted_version:
                        answers.append((qtype, address.packed))
            else:
                for value in zone.get(qtype_name, []):
                    address = ipaddress.ip_address(value)
                    wanted_version = 4 if qtype == 1 else 6 if qtype == 28 else None
                    if address.version == wanted_version:
                        answers.append((qtype, address.packed))
            next_zone = zone.pop("next", None) if qtype_name == "A" else None
            if next_zone is not None:
                self.zones[name] = next_zone
            return answers, rcode, ttl


STATE = DNSState()


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        packet, sock = self.request
        if len(packet) < 12:
            return
        try:
            name, offset = _read_name(packet, 12)
            qtype, qclass = struct.unpack_from("!HH", packet, offset)
        except (IndexError, struct.error, UnicodeDecodeError):
            return
        question_end = offset + 4
        if qclass != 1:
            rcode, answers, ttl = 4, [], 0
        else:
            answers, rcode, ttl = STATE.resolve(name, qtype)
            if rcode < 0:
                return
        question = packet[12:question_end]
        encoded_answers = bytearray()
        for answer_type, value in answers:
            if answer_type == 5:
                # The CNAME target becomes the owner of following A/AAAA data.
                target, _ = _read_name(value, 0)
                owner = _encode_name(name)
                rdata = value
            else:
                target = name
                owner = _encode_name(target)
                rdata = value
            encoded_answers.extend(owner)
            encoded_answers.extend(
                struct.pack("!HHIH", answer_type, 1, ttl, len(rdata))
            )
            encoded_answers.extend(rdata)
        # For a CNAME zone, record A/AAAA answers under its target name.
        if answers and answers[0][0] == 5:
            target = _read_name(answers[0][1], 0)[0]
            encoded_answers = bytearray()
            encoded_answers.extend(b"\xc0\x0c")
            encoded_answers.extend(struct.pack("!HHIH", 5, 1, ttl, len(answers[0][1])))
            encoded_answers.extend(answers[0][1])
            for answer_type, value in answers[1:]:
                encoded_answers.extend(_encode_name(target))
                encoded_answers.extend(
                    struct.pack("!HHIH", answer_type, 1, ttl, len(value))
                )
                encoded_answers.extend(value)
        query_flags = struct.unpack_from("!H", packet, 2)[0]
        flags = 0x8000 | 0x0080 | (query_flags & 0x0100) | rcode
        response = (
            packet[:2]
            + struct.pack("!HHHHH", flags, 1, len(answers), 0, 0)
            + question
            + bytes(encoded_answers)
        )
        sock.sendto(response, self.client_address)


class DNSUDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


class ControlHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        if self.path == "/state":
            with STATE.lock:
                return self._send(200, {"queries": list(STATE.queries)})
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/configure":
            return self._send(404, {"error": "not_found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 16_384:
                raise ValueError("invalid control payload size")
            value = json.loads(self.rfile.read(size))
            zones = value["zones"]
            if not isinstance(zones, dict):
                raise TypeError("zones must be an object")
            STATE.configure(zones)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "invalid_configuration"})
        return self._send(200, {"ok": True})

    def _send(self, status: int, value: dict):
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dns-address", default="0.0.0.0")
    parser.add_argument("--dns-port", type=int, default=53)
    parser.add_argument("--control-address", default="0.0.0.0")
    parser.add_argument("--control-port", type=int, default=8053)
    args = parser.parse_args()
    dns = DNSUDPServer((args.dns_address, args.dns_port), DNSHandler)
    control = ThreadingHTTPServer(
        (args.control_address, args.control_port), ControlHandler
    )
    threading.Thread(target=dns.serve_forever, daemon=True).start()
    control.serve_forever()


if __name__ == "__main__":
    main()
