"""Run a compiled live BotBuild in Scrapy through the gateway to controlled data."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def _assert_no_direct_route() -> None:
    for host, port in (("api.fixture.test", 443), ("controlled-source", 8080)):
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            continue
        for address in addresses:
            try:
                socket.create_connection(address[4], timeout=0.5).close()
            except OSError:
                continue
            raise SystemExit(
                f"live worker reached controlled source directly: {host}:{port}"
            )
    for port in (443, 8080):
        try:
            socket.create_connection(("172.30.249.10", port), timeout=0.5).close()
        except OSError:
            continue
        raise SystemExit(
            f"live worker reached controlled source IP directly: port {port}"
        )
    for persistence_host in ("postgres", "mongodb", "rustfs", "artifact-custody"):
        try:
            socket.getaddrinfo(persistence_host, None)
        except OSError:
            continue
        raise SystemExit(
            f"live worker resolved unauthorized persistence service: {persistence_host}"
        )
    try:
        host_gateway = socket.gethostbyname("host.docker.internal")
    except OSError:
        host_gateway = None
    if host_gateway is not None:
        ports = [55432, 27017, 8086]
        bc04_port = os.environ.get("MOZAIC_BC04_TEST_PORT")
        if bc04_port and bc04_port.isdigit():
            ports.append(int(bc04_port))
        for service_port in ports:
            try:
                socket.create_connection(
                    (host_gateway, service_port), timeout=0.5
                ).close()
            except OSError:
                continue
            raise SystemExit(
                f"live worker reached host persistence port {service_port}"
            )


def main() -> int:
    _assert_no_direct_route()
    build = Path("/opt/live-bot")
    if not (build / "manifest.json").is_file():
        raise SystemExit("compiled live BotBuild was not installed")
    retained_output = os.environ.get("MOZAIC_RESULT_DIRECTORY")
    if retained_output:
        output = Path(retained_output)
        if output.exists() or output.is_symlink():
            raise SystemExit("controlled live result directory must be empty and new")
        if output.parent.is_symlink() or not output.parent.is_dir():
            raise SystemExit(
                "controlled live result parent must be an existing safe directory"
            )
        _execute_build(build, output)
    else:
        with tempfile.TemporaryDirectory(prefix="dm-live-output-") as temporary:
            output = Path(temporary) / "result"
            _execute_build(build, output)
    return 0


def _execute_build(build: Path, output: Path) -> None:
    command = ["python", "-B", str(build / "bot.py"), "--output", str(output)]
    input_parameters = os.environ.get("MOZAIC_INPUT_PARAMETERS_FILE")
    if input_parameters:
        command.extend(("--input-parameters-file", input_parameters))
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode != 0:
            raise SystemExit(
                f"compiled Scrapy live BotBuild failed: {result.stdout[-1000:]} {result.stderr[-1000:]}"
            )
    finally:
        _make_harness_output_removable(output)

    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    records = [
        json.loads(line)
        for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    evidence = [
        json.loads(line)
        for line in (output / "fetch-evidence.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    artifact_only = (
        json.loads((build / "manifest.json").read_text())
        .get("output_contract", {})
        .get("mode")
        == "artifact"
    )
    if not evidence:
        raise SystemExit("compiled live BotBuild must produce gateway fetch evidence")
    if (
        report.get("requests") != len(evidence)
        or report.get("pages") != len(evidence)
        or report.get("records") != len(records)
        or (records if artifact_only else not records)
        or (artifact_only and report.get("output_mode") != "artifact")
    ):
        raise SystemExit(
            f"compiled live BotBuild report did not match the controlled acquisition: {report}"
        )
    fetch_urls = {item.get("source_url") for item in evidence}
    if not artifact_only and any(
        item.get("_provenance", {}).get("source_url") not in fetch_urls
        for item in records
    ):
        raise SystemExit("compiled live output lost the authorized source binding")
    if any(not item.get("body_sha256") for item in evidence) or sum(
        int(item.get("body_size_bytes", -1)) for item in evidence
    ) != report.get("response_bytes"):
        raise SystemExit(
            "compiled live BotBuild did not preserve gateway evidence metadata"
        )

    token = Path(os.environ["MOZAIC_EXECUTION_CAPABILITY_FILE"]).read_text().strip()
    request = urllib.request.Request(
        os.environ["MOZAIC_ACQUISITION_GATEWAY_URL"].rstrip("/") + "/v1/fetch",
        data=b'{"url":"https://unauthorized.invalid/"}',
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=3)
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise SystemExit(
                "gateway did not reject the unauthorized origin"
            ) from error
    else:
        raise SystemExit("gateway accepted the unauthorized origin")
    print(
        "Compiled live BotBuild acquired the selected controlled HTTP payload through Go gateway; direct route and unauthorized origin denied"
    )


def _make_harness_output_removable(output: Path) -> None:
    if output.is_symlink() or not output.is_dir():
        return
    for path in output.iterdir():
        if path.is_file() and not path.is_symlink():
            path.chmod(0o644)
    # The harness writes inside a private 0700 TemporaryDirectory. Keep the
    # result group-writable there so the host runner can remove files created
    # by the unprivileged container UID during bounded test cleanup.
    output.chmod(0o777)


if __name__ == "__main__":
    raise SystemExit(main())
