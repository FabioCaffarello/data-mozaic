"""Operator-only negative test for managed MCP identity enforcement.

Requires a live trusted broker and an already provisioned Codex requirements
allowlist. It creates only disposable configuration and a marker in a temp dir.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

BROKER_URL = "http://127.0.0.1:8765/mcp"


def main() -> int:
    token = os.environ.get("SCOUT_BROKER_TOKEN", "")
    codex = os.environ.get("CODEX_BIN", "codex")
    if not token:
        raise SystemExit(
            "SCOUT_BROKER_TOKEN must be supplied through the operator's protected environment"
        )
    with tempfile.TemporaryDirectory(prefix="codex-mcp-allowlist-probe-") as temporary:
        root = Path(temporary)
        home = root / "codex-home"
        project = root / "project"
        (project / ".codex").mkdir(parents=True)
        marker = root / "unapproved-stdio-was-started"
        marker_code = (
            f"from pathlib import Path; Path({str(marker)!r}).write_text('started')"
        )

        def config_for(url: str) -> str:
            return "\n".join(
                (
                    "[mcp_servers.data_mozaic_scout]",
                    f'url = "{url}"',
                    'bearer_token_env_var = "SCOUT_BROKER_TOKEN"',
                    "required = true",
                    "",
                    "[mcp_servers.unapproved_stdio_probe]",
                    f"command = {os.sys.executable!r}",
                    f'args = ["-c", {marker_code!r}]',
                    "",
                )
            )

        config_path = project / ".codex/config.toml"
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        attempts = (
            ("approved URL startup", BROKER_URL, False),
            (
                "wrong URL after project-config rewrite",
                "http://127.0.0.1:8766/mcp",
                True,
            ),
        )
        for attempt, configured_url, require_disabled_status in attempts:
            config_path.write_text(config_for(configured_url), encoding="utf-8")
            result = subprocess.run(
                [codex, "mcp", "list"],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            output = result.stdout + result.stderr
            if result.returncode:
                raise SystemExit(
                    f"codex mcp list failed during {attempt}; inspect output without exposing credentials"
                )
            if "data_mozaic_scout" not in output:
                raise SystemExit(
                    f"approved broker identity was not visible during {attempt}"
                )
            if require_disabled_status:
                lines = output.lower().splitlines()
                server_line = next(
                    index
                    for index, line in enumerate(lines)
                    if "data_mozaic_scout" in line
                )
                status_lines = lines[server_line : server_line + 3]
                if not any("disabled" in line for line in status_lines):
                    raise SystemExit(
                        "Codex output did not prove the wrong-URL MCP server was disabled; "
                        "inspect effective policy status manually"
                    )
            if marker.exists():
                raise SystemExit(
                    f"managed MCP policy failed: synthetic STDIO executed during {attempt}"
                )
        print(
            "synthetic unapproved STDIO marker absent; wrong URL rejected across "
            "two Codex startups"
        )
        print(
            "operator must retain CLI output and independently confirm the effective requirements source"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
