"""Path and invocation regressions for the local Codex launchers."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CodexToolingTests(unittest.TestCase):
    def test_mcp_launcher_handles_spaces_and_arbitrary_invocation_directory(self):
        with tempfile.TemporaryDirectory(prefix="checkout with spaces ") as temporary:
            base = Path(temporary)
            checkout = base / "repo root with spaces"
            (checkout / "tooling").mkdir(parents=True)
            (checkout / ".local").mkdir()
            launcher = checkout / "tooling/codex-scout-mcp.sh"
            shutil.copy2(ROOT / "tooling/codex-scout-mcp.sh", launcher)
            (checkout / ".local/scout-mcp.env").write_text(
                "SCOUT_LOCAL_PRINCIPAL=private-test-marker\n"
            )
            fake_bin = base / "bin with spaces"
            fake_bin.mkdir()
            capture = base / "launcher args.json"
            fake_uv = fake_bin / "uv"
            fake_uv.write_text(
                "#!/bin/sh\n"
                "python3 -c 'import json,os,sys; "
                'open(os.environ["CAPTURE_ARGS"],"w").write(json.dumps({"cwd":os.getcwd(),"args":sys.argv[1:]}))\' "$@"\n'
            )
            fake_uv.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["CAPTURE_ARGS"] = str(capture)
            result = subprocess.run(
                [str(launcher)],
                cwd=base,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            invocation = json.loads(capture.read_text())
            self.assertEqual(
                str(checkout.resolve()), str(Path(invocation["cwd"]).resolve())
            )
            self.assertEqual(
                ".local/scout-mcp.env",
                invocation["args"][invocation["args"].index("--env-file") + 1],
            )
            self.assertNotIn("private-test-marker", " ".join(invocation["args"]))

    def test_mcp_launcher_fails_closed_when_configuration_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="missing config spaces ") as temporary:
            checkout = Path(temporary) / "repo with spaces"
            (checkout / "tooling").mkdir(parents=True)
            launcher = checkout / "tooling/codex-scout-mcp.sh"
            shutil.copy2(ROOT / "tooling/codex-scout-mcp.sh", launcher)
            result = subprocess.run(
                [str(launcher)],
                cwd=Path(temporary),
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("Missing", result.stderr)

    def test_bc15_wrapper_runs_all_commands_from_external_paths_with_spaces(self):
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
            report = session / "replay report.json"
            replay_output = session / "replay output"
            acceptance_output = session / "acceptance output"
            acceptance_report = session / "acceptance report.json"
            expectations = fixture / "acceptance.json"
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
                "--report",
                report,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            result = run(
                "accept",
                playbook,
                transcript,
                "--fixtures",
                fixture,
                "--expectations",
                expectations,
                "--output",
                acceptance_output,
                "--report",
                acceptance_report,
                cwd=base,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                "accepted",
                json.loads(acceptance_report.read_text())["acquisition_acceptance"][
                    "state"
                ],
            )


if __name__ == "__main__":
    unittest.main()
