"""Path and invocation regressions for the local Codex launchers."""

import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CodexToolingTests(unittest.TestCase):
    def test_project_mcp_config_requires_loopback_http_and_no_stdio(self):
        config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
        server = config["mcp_servers"]["data_mozaic_scout"]
        self.assertEqual("http://127.0.0.1:8765/mcp", server["url"])
        self.assertEqual("SCOUT_BROKER_TOKEN", server["bearer_token_env_var"])
        self.assertTrue(server["required"])
        self.assertNotIn("command", server)
        self.assertNotIn("args", server)
        self.assertNotIn("env_vars", server)
        requirements = tomllib.loads(
            (ROOT / "config/scout/codex-requirements.example.toml").read_text()
        )
        self.assertEqual(
            server["url"],
            requirements["mcp_servers"]["data_mozaic_scout"]["identity"]["url"],
        )

    def test_workspace_stdio_launcher_is_disabled(self):
        with tempfile.TemporaryDirectory(prefix="checkout with spaces ") as temporary:
            base = Path(temporary)
            checkout = base / "repo root with spaces"
            (checkout / "tooling").mkdir(parents=True)
            launcher = checkout / "tooling/codex-scout-mcp.sh"
            shutil.copy2(ROOT / "tooling/codex-scout-mcp.sh", launcher)
            launcher.chmod(0o755)
            result = subprocess.run(
                [str(launcher)],
                cwd=base,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(78, result.returncode)
            self.assertIn("STDIO startup is disabled", result.stderr)

    def test_bc15_wrapper_restricts_external_callers_to_draft_and_validate(self):
        with tempfile.TemporaryDirectory(prefix="bc15 work with spaces ") as temporary:
            base = Path(temporary)
            session = base / "session artifacts"
            external_cwd = base / "unrelated working directory"
            session.mkdir()
            external_cwd.mkdir()
            shutil.copytree(
                ROOT / "tests/fixtures/collector-engineering/multipage",
                session / "fixture",
            )
            fixture = session / "fixture"
            transcript = fixture / "transcript.json"
            playbook = session / "capture playbook.yaml"
            replay_output = session / "replay output"
            wrapper = ROOT / "tooling/codex-scout-bc15.sh"

            def run(*args, cwd=external_cwd):
                return subprocess.run(
                    [str(wrapper), *map(str, args)],
                    cwd=cwd,
                    check=False,
                    capture_output=True,
                    text=True,
                    env={**os.environ, "UV_OFFLINE": "1"},
                )

            result = run("draft", transcript, "--output", playbook)
            self.assertEqual(0, result.returncode, result.stderr)
            result = run("validate", playbook, cwd=session)
            self.assertEqual(0, result.returncode, result.stderr)
            result = run(
                "replay",
                playbook,
                "--fixtures",
                fixture,
                "--output",
                replay_output,
            )
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn("invalid choice", result.stderr)
            self.assertFalse(replay_output.exists())
            result = run(
                "accept",
                playbook,
                transcript,
                "--fixtures",
                fixture,
                cwd=base,
            )
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn("invalid choice", result.stderr)


if __name__ == "__main__":
    unittest.main()
