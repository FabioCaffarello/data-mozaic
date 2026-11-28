"""Run controlled Scout proposals through raw, structured, and parameterized publication."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, env: dict[str, str]) -> str:
    print("$ " + " ".join(command), flush=True)
    result = subprocess.run(
        command, cwd=ROOT, env=env, check=False, text=True, capture_output=True
    )
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, command)
    return result.stdout


def main() -> int:
    if not os.environ.get("SCOUT_DATABASE_URL"):
        raise SystemExit("SCOUT_DATABASE_URL must come from the reviewed local .env")
    required = (
        "BC04_DATABASE_URL",
        "BC04_GRANT_SIGNING_KEY",
        "BC04_GRANT_KEY_ID",
        "ARTIFACT_CUSTODY_LOCAL_TOKEN",
        "ARTIFACT_CUSTODY_HTTP_URL",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit("load the preserved local execution and custody configuration")
    scratch = ROOT / ".local"
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-to-raw-", dir=scratch) as directory:
        candidate_dir = Path(directory)
        env = os.environ.copy()
        env["SCOUT_HTTP_E2E_CANDIDATE_DIR"] = str(candidate_dir)
        scout_output = run(
            [sys.executable, "tooling/run_scout_http_e2e.py", "--suite", "http"],
            env=env,
        )
        env["MOZAIC_LIFECYCLE_ACCEPTANCE"] = "1"
        candidate_provenance = {}
        for name in (
            "raw-provenance.json",
            "structured.json.yaml.provenance.json",
            "parameterized.yaml.provenance.json",
        ):
            source = candidate_dir / name
            if not source.is_file() or source.is_symlink():
                raise RuntimeError(f"Scout journey provenance missing: {name}")
            candidate_provenance[name] = json.loads(source.read_text(encoding="utf-8"))
        acquisitions = {}
        for case, candidate_name in (
            ("raw-pdf", "raw.yaml"),
            ("json-work-intake", "structured.json.yaml"),
            ("work-intake", "parameterized.yaml"),
        ):
            candidate = candidate_dir / candidate_name
            if candidate.is_symlink() or not candidate.is_file():
                raise RuntimeError(f"Scout E2E did not export the {case} proposal")
            candidate_bytes = candidate.read_bytes()
            if not 0 < len(candidate_bytes) <= 1_000_000:
                raise RuntimeError(f"{case} proposal exceeds the bounded size")
            print(
                f"Scout {case} candidate sha256="
                + hashlib.sha256(candidate_bytes).hexdigest(),
                flush=True,
            )
            output = run(
                [
                    sys.executable,
                    "tooling/run_authorized_live_json.py",
                    "--case",
                    case,
                    "--playbook",
                    str(candidate),
                ],
                env=env,
            )
            accepted = []
            for line in output.splitlines():
                marker, separator, payload = line.partition(" ")
                if separator and (
                    marker.startswith("AUTHORIZED_CONTROLLED_LIVE_")
                    or marker == "WORK_INTAKE_THREE_INPUTS_CORRELATED"
                ):
                    try:
                        accepted.append(
                            {"marker": marker, "result": json.loads(payload)}
                        )
                    except json.JSONDecodeError:
                        continue
            if not accepted:
                raise RuntimeError(f"{case} acquisition produced no accepted outcome")
            acquisitions[case] = accepted
        evidence_path = (
            ROOT
            / ".local"
            / (
                "agent-to-raw-evidence-"
                + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + uuid.uuid4().hex[:8]
                + ".json"
            )
        )
        evidence_path.write_text(
            json.dumps(
                {
                    "contract": "agent-to-raw-acceptance-evidence/v1",
                    "scout_e2e_passed": "verified: http suite passed" in scout_output,
                    "candidate_provenance": candidate_provenance,
                    "acquisition_outcomes": acquisitions,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        evidence_path.chmod(0o600)
        print(f"AGENT_TO_RAW_EVIDENCE {evidence_path.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
