"""Run the real Scout MCP HTTP path against an isolated Compose HTTPS fixture."""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from argparse import ArgumentParser
from pathlib import Path
from unittest import TestLoader, TextTestRunner
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deployments/compose/scout-http-e2e.compose.yaml"


def _run(command: list[str], *, env: dict[str, str], check: bool = True):
    return subprocess.run(command, cwd=ROOT, env=env, check=check, text=True)


def _build_test_worker_image() -> tuple[str, str, str]:
    """Build an isolated test image before starting the test MCP subprocess."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker is required for the Scout HTTP integration suite")
    suffix = uuid4().hex[:16]
    worker_tag = f"data-mozaic-bc15-http-test:{suffix}"
    base_tag = f"data-mozaic-python-http-test:{suffix}"
    base = subprocess.run(
        [docker, "image", "inspect", "python:3.12-slim", "--format", "{{.Id}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if base.returncode:
        subprocess.run([docker, "pull", "python:3.12-slim"], check=True)
        base = subprocess.run(
            [docker, "image", "inspect", "python:3.12-slim", "--format", "{{.Id}}"],
            text=True,
            capture_output=True,
            check=True,
        )
    base_id = base.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", base_id):
        raise RuntimeError("test base image did not resolve to an immutable ID")
    subprocess.run([docker, "image", "tag", base_id, base_tag], check=True)
    with tempfile.TemporaryDirectory(prefix="scout-http-worker-") as temp:
        context = Path(temp)
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--package",
                "mozaic-collector-engineering",
                "--no-dev",
                "--no-emit-workspace",
                "--no-emit-package",
                "mozaic-collector-engineering",
                "--format",
                "requirements.txt",
                "--output-file",
                str(context / "requirements.txt"),
            ],
            cwd=ROOT,
            env=os.environ.copy(),
            check=True,
            text=True,
            capture_output=True,
        )
        shutil.copytree(
            ROOT / "apps/collector-engineering/collector_engineering",
            context / "collector_engineering",
        )
        shutil.copy2(
            ROOT / "apps/collector-engineering/Dockerfile.protected",
            context / "Dockerfile.protected",
        )
        subprocess.run(
            [
                docker,
                "build",
                "--quiet",
                "--build-arg",
                f"PYTHON_BASE_IMAGE={base_tag}",
                "--tag",
                worker_tag,
                "--file",
                str(context / "Dockerfile.protected"),
                str(context),
            ],
            cwd=ROOT,
            check=True,
            timeout=300,
        )
    worker_id = subprocess.check_output(
        [docker, "image", "inspect", worker_tag, "--format", "{{.Id}}"], text=True
    ).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", worker_id):
        raise RuntimeError("test worker image did not resolve to an immutable ID")
    return worker_tag, base_tag, worker_id


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
    return _free_loopback_port()


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _select_e2e_subnet() -> ipaddress.IPv4Network:
    network_ids = subprocess.run(
        ["docker", "network", "ls", "--quiet"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    occupied = []
    if network_ids:
        inspected = subprocess.run(
            ["docker", "network", "inspect", *network_ids],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for network in json.loads(inspected):
            for config in network.get("IPAM", {}).get("Config") or []:
                subnet = config.get("Subnet")
                if subnet:
                    try:
                        occupied.append(ipaddress.ip_network(subnet, strict=False))
                    except ValueError:
                        continue
    for third_octet in range(256):
        candidate = ipaddress.ip_network(f"172.30.{third_octet}.0/24")
        if not any(candidate.overlaps(existing) for existing in occupied):
            return candidate
    raise RuntimeError("no unused Docker subnet is available for Scout E2E")


def _render_squid_config(destination: Path, *, fixture_ip: str, dns_ip: str) -> None:
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
    fixture = ipaddress.ip_network(f"{fixture_ip}/32")
    ranges = []
    for item in blocked:
        network = ipaddress.ip_network(item)
        if fixture.version == network.version and fixture.subnet_of(network):
            ranges.extend(network.address_exclude(fixture))
        else:
            ranges.append(network)
    template = (ROOT / "config/scout/squid-e2e.conf").read_text(encoding="utf-8")
    destination.write_text(
        template.replace("172.30.91.53", dns_ip).replace(
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


def _log_time_ns(fields: list[str]) -> int:
    seconds, milliseconds = fields[0].split(".", 1)
    return (
        int(seconds) * 1_000_000_000 + int(milliseconds[:3].ljust(3, "0")) * 1_000_000
    )


def _records_in_operation(
    records: list[list[str]], host: str, operation: dict
) -> list[list[str]]:
    start_ns = operation["started_ns"] - 5_000_000_000
    end_ns = operation["finished_ns"] + 5_000_000_000
    return [
        fields
        for fields in records
        if len(fields) >= 7
        and fields[2] in {host, f"{host}:443"}
        and start_ns <= _log_time_ns(fields) <= end_ns
    ]


def _dns_correlation_is_valid(operation: dict, host: str) -> bool:
    queries = {
        (item["id"], item["ordinal"], item["name"], item["type"])
        for item in operation["dns_queries"]
    }
    return all(
        (
            item["id"],
            item["ordinal"],
            item["name"],
            item["type"],
        )
        in queries
        and item["name"] == host
        for item in operation["dns_responses"]
        if item["name"] == host
    )


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--suite", choices=("http", "dns"), required=True)
    args = parser.parse_args()
    if not os.environ.get("SCOUT_DATABASE_URL"):
        raise RuntimeError("SCOUT_DATABASE_URL must come from the reviewed local .env")
    project = os.environ.get("SCOUT_HTTP_E2E_PROJECT") or (
        "scout-http-e2e-" + uuid4().hex[:10]
    )
    subnet = _select_e2e_subnet()
    addresses = {
        "SCOUT_HTTP_E2E_SUBNET": str(subnet),
        "SCOUT_HTTP_E2E_SQUID_IP": str(subnet.network_address + 2),
        "SCOUT_HTTP_E2E_FIXTURE_IP": str(subnet.network_address + 10),
        "SCOUT_HTTP_E2E_SINK_IP": str(subnet.network_address + 11),
        "SCOUT_HTTP_E2E_DNS_IP": str(subnet.network_address + 53),
    }
    port = _test_proxy_port()
    dns_control_port = _free_loopback_port()
    worker_tag, base_tag, worker_image = _build_test_worker_image()
    with tempfile.TemporaryDirectory(prefix="scout-http-e2e-") as temporary:
        temporary = str(Path(temporary).resolve())
        cert_dir = Path(temporary) / "certs"
        cert_dir.mkdir()
        _make_certificates(cert_dir)
        squid_config = Path(temporary) / "squid.conf"
        _render_squid_config(
            squid_config,
            fixture_ip=addresses["SCOUT_HTTP_E2E_FIXTURE_IP"],
            dns_ip=addresses["SCOUT_HTTP_E2E_DNS_IP"],
        )
        env = os.environ.copy()
        env.update(
            {
                **addresses,
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
                "SCOUT_TEST_WORKER_IMAGE_DIGEST": worker_image,
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
            os.environ.update(env)
            if args.suite == "http":
                test_names = (
                    "test_authorized_https_mcp_custody_transcript_and_restart_replay",
                    "test_proxy_denies_private_destination_tls_failure_and_unavailable_proxy",
                )
            else:
                test_names = (
                    "test_squid_resolves_pins_and_denies_adversarial_dns_answers",
                    "test_mcp_dns_failure_then_forbidden_recovery_and_allowed_control",
                )
            test_module_path = ROOT / "tests/e2e/test_scout_http_mcp.py"
            module_spec = importlib.util.spec_from_file_location(
                "scout_http_mcp_e2e_tests", test_module_path
            )
            if module_spec is None or module_spec.loader is None:
                raise RuntimeError("could not load the Scout HTTP E2E test module")
            test_module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(test_module)
            test_suite = TestLoader().loadTestsFromTestCase(
                test_module.ScoutHTTPMcpE2ETests
            )
            selected = {test.id().rsplit(".", 1)[-1]: test for test in test_suite}
            suite = test_module.unittest.TestSuite(
                selected[name] for name in test_names if name in selected
            )
            if suite.countTestCases() != len(test_names):
                raise RuntimeError("requested E2E test selection is incomplete")
            test_result = TextTestRunner(verbosity=2).run(suite)
            test_passed = (
                test_result.wasSuccessful()
                and test_result.testsRun == len(test_names)
                and not test_result.skipped
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
            fixture_page_hits = {
                "/docs/page": logs.count("fixture.request method=GET path=/docs/page"),
                "/docs/next": logs.count("fixture.request method=GET path=/docs/next"),
            }
            expected_fixture_page_hits = (
                {"/docs/page": 1, "/docs/next": 1}
                if args.suite == "http"
                else {"/docs/page": 1, "/docs/next": 0}
            )
            access_records = [line.split() for line in squid_access.splitlines()]
            squid_version_match = re.search(
                r"Starting Squid Cache version ([0-9.]+)", squid_cache
            )
            squid_version = (
                squid_version_match.group(1) if squid_version_match else None
            )
            positive_dns_records = [
                fields
                for fields in access_records
                if len(fields) >= 7
                and fields[2] in {"catalog.fixture.test", "catalog.fixture.test:443"}
                and fields[3] == addresses["SCOUT_HTTP_E2E_FIXTURE_IP"]
                and fields[4] == "HIER_DIRECT"
                and fields[5] == "TCP_TUNNEL/200"
                and fields[6] == "CONNECT"
            ]
            denied_count = squid_access.count("TCP_DENIED/403 CONNECT")
            sink_hits = logs.count("egress-sink.accept")
            dns_evidence_ok = args.suite != "dns"
            dns_evidence = None
            if args.suite == "dns":
                evidence_path = Path(temporary) / "dns-recovery-evidence.json"
                if evidence_path.is_file():
                    dns_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                    recovery_host = dns_evidence["recovery_host"]
                    first_operation = dns_evidence["first_operation"]
                    first_records = _records_in_operation(
                        access_records, recovery_host, first_operation
                    )
                    first_responses = first_operation["dns_responses"]
                    first_a = [
                        item
                        for item in first_responses
                        if item["name"] == recovery_host and item["type"] == "A"
                    ]
                    first_has_forbidden = any(
                        answer.get("value") == addresses["SCOUT_HTTP_E2E_SINK_IP"]
                        for item in first_responses
                        for answer in item["answers"]
                    )
                    first_private_after_failure = any(
                        item["name"] == recovery_host
                        and item["type"] == "A"
                        and item["rcode"] == 0
                        and item["ordinal"] > first_a[0]["ordinal"]
                        and any(
                            answer.get("value") == addresses["SCOUT_HTTP_E2E_SINK_IP"]
                            for answer in item["answers"]
                        )
                        for item in first_responses
                    )
                    first_transaction_ids = {fields[1] for fields in first_records}
                    first_policy_denies = (
                        len(first_records) == 1
                        and len(first_transaction_ids) == 1
                        and first_records[0][3] == "-"
                        and first_records[0][5] == "TCP_DENIED/403"
                        and first_records[0][6] == "CONNECT"
                    )
                    positive_control = dns_evidence["positive_control"]
                    positive_control_records = _records_in_operation(
                        access_records, "catalog.fixture.test", positive_control
                    )
                    positive_control_connected = any(
                        fields[3] == addresses["SCOUT_HTTP_E2E_FIXTURE_IP"]
                        and fields[4] == "HIER_DIRECT"
                        and fields[5] == "TCP_TUNNEL/200"
                        and fields[6] == "CONNECT"
                        for fields in positive_control_records
                    )
                    dns_evidence_ok = (
                        bool(first_a)
                        and first_a[0]["rcode"] == 2
                        and first_operation["mcp_result"]["ok"] is False
                        and first_operation["mcp_result"]["error"]["code"]
                        == "policy_denied"
                        and first_private_after_failure
                        and first_has_forbidden
                        and first_policy_denies
                        and _dns_correlation_is_valid(first_operation, recovery_host)
                        and positive_control["mcp_result"]["ok"] is True
                        and positive_control_connected
                        and bool(first_transaction_ids)
                        and first_transaction_ids.isdisjoint(
                            {fields[1] for fields in positive_control_records}
                        )
                    )
                    dns_evidence["squid_transactions"] = [
                        {
                            "master_xaction": fields[1],
                            "request_target": fields[2],
                            "peer_ip": fields[3],
                            "hierarchy": fields[4],
                            "result": fields[5],
                            "method": fields[6],
                        }
                        for fields in access_records
                        if len(fields) >= 7
                        and fields[2] in {recovery_host, f"{recovery_host}:443"}
                    ]
                    dns_evidence["squid_transactions"].extend(
                        {
                            "master_xaction": fields[1],
                            "request_target": fields[2],
                            "peer_ip": fields[3],
                            "hierarchy": fields[4],
                            "result": fields[5],
                            "method": fields[6],
                        }
                        for fields in positive_control_records
                    )
            min_denied = 5 if args.suite == "dns" else 0
            if (
                test_passed
                and fixture_page_hits == expected_fixture_page_hits
                and bool(positive_dns_records)
                and squid_version == "7.7"
                and denied_count >= min_denied
                and sink_hits == 0
                and dns_evidence_ok
            ):
                connection_evidence = " ".join(positive_dns_records[0])
                print(
                    f"verified: {args.suite} suite passed with {test_result.testsRun} "
                    f"tests and zero skips on Squid {squid_version}; fixture GETs={fixture_page_hits}; Squid connected "
                    f"to fixture IP {addresses['SCOUT_HTTP_E2E_FIXTURE_IP']}; "
                    "forbidden sink accepted zero connections"
                )
                print(f"connection evidence: {connection_evidence}")
                print(f"denied CONNECTs: {denied_count}; forbidden sink accepts: 0")
                if dns_evidence is not None:
                    print(
                        "DNS recovery evidence: "
                        + json.dumps(dns_evidence, separators=(",", ":"))
                    )
                status = 0
            else:
                print(logs)
                print(squid_access)
                print(squid_cache)
                print(dns_state)
                print(
                    "e2e evidence check failed: "
                    f"tests_passed={test_passed}, tests_run={test_result.testsRun}, "
                    f"skips={len(test_result.skipped)}, fixture_page_hits={fixture_page_hits}, "
                    f"connected_fixture_ip={bool(positive_dns_records)}, "
                    f"squid_version={squid_version}, "
                    f"denied={denied_count}, sink_hits={sink_hits}, "
                    f"dns_evidence={dns_evidence_ok}"
                )
        finally:
            _run([*compose, "down"], env=env, check=False)
            docker = shutil.which("docker")
            if docker:
                subprocess.run(
                    [docker, "image", "rm", "--force", worker_tag, base_tag],
                    check=False,
                    capture_output=True,
                )
        return status


if __name__ == "__main__":
    raise SystemExit(main())
