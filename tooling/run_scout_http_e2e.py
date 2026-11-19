"""Run the real Scout MCP HTTP path against an isolated Compose HTTPS fixture."""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deployments/compose/scout-http-e2e.compose.yaml"


def _run(command: list[str], *, env: dict[str, str], check: bool = True):
    return subprocess.run(command, cwd=ROOT, env=env, check=check, text=True)


def _make_certificates(directory: Path) -> None:
    ca_key = directory / "ca.key"
    ca_cert = directory / "ca.crt"
    server_key = directory / "server.key"
    server_csr = directory / "server.csr"
    server_cert = directory / "server.crt"
    extensions = directory / "server.ext"
    extensions.write_text(
        "subjectAltName=DNS:catalog.fixture.test,DNS:*.fixture.test\n"
        "extendedKeyUsage=serverAuth\n"
        "keyUsage=digitalSignature,keyEncipherment\n",
        encoding="ascii",
    )
    _run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_cert),
            "-days",
            "1",
            "-subj",
            "/CN=DataMozaic-Scout-E2E-CA",
            "-sha256",
        ],
        env=os.environ.copy(),
    )
    _run(
        [
            "openssl",
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(server_key),
            "-out",
            str(server_csr),
            "-subj",
            "/CN=catalog.fixture.test",
            "-sha256",
        ],
        env=os.environ.copy(),
    )
    _run(
        [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(server_csr),
            "-CA",
            str(ca_cert),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(server_cert),
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(extensions),
        ],
        env=os.environ.copy(),
    )
    for path in (ca_cert, server_cert, server_key):
        path.chmod(0o644)


def _test_proxy_port() -> int:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", 3128))
        except OSError as error:
            raise RuntimeError(
                "the isolated E2E proxy requires free loopback port 3128"
            ) from error
        return 3128


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _render_squid_config(destination: Path) -> None:
    blocked = (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
        "5f00::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
    fixture = ipaddress.ip_network("172.30.91.10/32")
    ranges = []
    for item in blocked:
        network = ipaddress.ip_network(item)
        if fixture.version == network.version and fixture.subnet_of(network):
            ranges.extend(network.address_exclude(fixture))
        else:
            ranges.append(network)
    template = (ROOT / "config/scout/squid-e2e.conf").read_text(encoding="utf-8")
    destination.write_text(
        template.replace(
            "@BLOCKED_DESTINATIONS@",
            " ".join(str(network) for network in ranges),
        ),
        encoding="utf-8",
    )


def _wait_proxy(port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("isolated test proxy did not become ready")


def _wait_dns_control(url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with urlopen(url + "/health", timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.2)
    raise RuntimeError("controlled DNS fixture did not become ready")


def main() -> int:
    if not os.environ.get("SCOUT_DATABASE_URL"):
        raise RuntimeError("SCOUT_DATABASE_URL must come from the reviewed local .env")
    project = "scout-http-e2e-" + uuid4().hex[:10]
    port = _test_proxy_port()
    dns_control_port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="scout-http-e2e-") as temporary:
        cert_dir = Path(temporary) / "certs"
        cert_dir.mkdir()
        _make_certificates(cert_dir)
        squid_config = Path(temporary) / "squid.conf"
        _render_squid_config(squid_config)
        env = os.environ.copy()
        env.update(
            {
                "SCOUT_HTTP_E2E_CERT_DIR": str(cert_dir),
                "SCOUT_HTTP_E2E_PROXY_PORT": str(port),
                "SCOUT_HTTP_E2E_DNS_CONTROL_PORT": str(dns_control_port),
                "SCOUT_HTTP_E2E": "1",
                "SCOUT_HTTP_E2E_TEMP": temporary,
                "SCOUT_HTTP_E2E_PROXY_URL": f"http://127.0.0.1:{port}",
                "SCOUT_HTTP_E2E_DNS_CONTROL_URL": f"http://127.0.0.1:{dns_control_port}",
                "SCOUT_HTTP_E2E_CA_FILE": str(cert_dir / "ca.crt"),
                "SCOUT_HTTP_E2E_SQUID_CONFIG": str(squid_config),
                "SCOUT_HTTP_E2E_DATABASE_URL": env["SCOUT_DATABASE_URL"],
            }
        )
        compose = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(COMPOSE),
        ]
        _run([*compose, "config", "--quiet"], env=env)
        status = 1
        try:
            _run([*compose, "up", "--detach", "--wait"], env=env)
            _wait_proxy(port)
            _wait_dns_control(env["SCOUT_HTTP_E2E_DNS_CONTROL_URL"])
            test = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "tests.e2e.test_scout_http_mcp",
                    "-v",
                ],
                cwd=ROOT,
                env=env,
                check=False,
                text=True,
            )
            logs = subprocess.run(
                [*compose, "logs", "--no-color"],
                cwd=ROOT,
                env=env,
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            squid_access = subprocess.run(
                [*compose, "exec", "--no-TTY", "squid", "cat", "/tmp/scout_access.log"],
                cwd=ROOT,
                env=env,
                check=False,
                text=True,
                capture_output=True,
            ).stdout
            squid_cache = subprocess.run(
                [
                    *compose,
                    "exec",
                    "--no-TTY",
                    "squid",
                    "cat",
                    "/var/log/squid/cache.log",
                ],
                cwd=ROOT,
                env=env,
                check=False,
                text=True,
                capture_output=True,
            ).stdout
            dns_state = subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    env["SCOUT_HTTP_E2E_DNS_CONTROL_URL"] + "/state",
                ],
                cwd=ROOT,
                env=env,
                check=False,
                text=True,
                capture_output=True,
            ).stdout
            fixture_hits = logs.count("fixture.request method=GET path=/docs/page")
            positive_dns_hit = (
                "172.30.91.10 HIER_DIRECT TCP_TUNNEL/200 CONNECT" in squid_access
            )
            denied_count = squid_access.count("TCP_DENIED/403 CONNECT")
            sink_hits = logs.count("egress-sink.accept")
            if (
                test.returncode == 0
                and fixture_hits == 1
                and positive_dns_hit
                and denied_count >= 5
                and sink_hits == 0
            ):
                connection_evidence = next(
                    line
                    for line in squid_access.splitlines()
                    if "172.30.91.10 HIER_DIRECT TCP_TUNNEL/200 CONNECT" in line
                )
                print(
                    "verified: MCP E2E passed; one /docs/page GET; Squid connected "
                    "to fixture IP 172.30.91.10; DNS deny cases were logged; "
                    "forbidden sink received no connections"
                )
                print(f"connection evidence: {connection_evidence}")
                print(f"denied CONNECTs: {denied_count}; forbidden sink accepts: 0")
                status = 0
            else:
                print(logs)
                print(squid_access)
                print(squid_cache)
                print(dns_state)
                print(
                    "e2e evidence check failed: "
                    f"test_exit={test.returncode}, fixture_hits={fixture_hits}, "
                    f"connected_fixture_ip={positive_dns_hit}, "
                    f"denied={denied_count}, sink_hits={sink_hits}"
                )
        finally:
            _run([*compose, "down"], env=env, check=False)
        return status


if __name__ == "__main__":
    raise SystemExit(main())
