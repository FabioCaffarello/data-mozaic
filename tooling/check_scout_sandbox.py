"""Run the optional Docker network-isolation proof and clean up its network."""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deployments/compose/scout-tool-sandbox.compose.yaml"


def main() -> int:
    project_name = f"scout-sandbox-check-{uuid4().hex[:10]}"
    prefix = [
        "docker",
        "compose",
        "--project-name",
        project_name,
        "--file",
        str(COMPOSE),
    ]
    env = os.environ | {
        "SCOUT_EGRESS_DOMAINS_FILE": str(
            ROOT / "config/scout/egress-domains.example.txt"
        )
    }
    subprocess.run([*prefix, "config", "--quiet"], check=True, cwd=ROOT, env=env)
    try:
        subprocess.run(
            [*prefix, "up", "--detach", "squid"], check=True, cwd=ROOT, env=env
        )
        subprocess.run(
            [*prefix, "run", "--rm", "--no-deps", "sandbox-probe"],
            check=True,
            cwd=ROOT,
            env=env,
        )
    except subprocess.CalledProcessError as error:
        return error.returncode
    finally:
        subprocess.run([*prefix, "down"], check=False, cwd=ROOT, env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
