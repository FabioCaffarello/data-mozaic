"""Wheel and sdist are usable from outside the monorepo checkout."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "apps/collector-engineering"
FIXTURE = ROOT / "tests/fixtures/collector-engineering/multipage"


class CollectorEngineeringPackagingTests(unittest.TestCase):
    def test_wheel_and_sdist_install_and_run_cli_outside_checkout(self):
        expected = {
            "CapturePlaybook.schema.json": hashlib.sha256(
                (
                    ROOT
                    / "contracts/capture-playbook/draft/v1/CapturePlaybook.schema.json"
                ).read_bytes()
            ).hexdigest(),
            "PlaybookProposal.schema.json": hashlib.sha256(
                (ROOT / "contracts/scout/v1/PlaybookProposal.schema.json").read_bytes()
            ).hexdigest(),
            "ScoutSessionTranscript.schema.json": hashlib.sha256(
                (
                    ROOT
                    / "contracts/scout/transcript/draft/v1/ScoutSessionTranscript.schema.json"
                ).read_bytes()
            ).hexdigest(),
        }
        with tempfile.TemporaryDirectory(prefix="bc15-isolated-") as temporary:
            external = Path(temporary)
            distributions = external / "dist"
            subprocess.run(
                [
                    "uv",
                    "build",
                    "--package",
                    "mozaic-collector-engineering",
                    "--out-dir",
                    str(distributions),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            fixture_copy = external / "fixtures"
            shutil.copytree(FIXTURE, fixture_copy)
            transcript_path = fixture_copy / "transcript.json"

            for suffix, distribution in (
                ("wheel", next(distributions.glob("*.whl"))),
                ("sdist", next(distributions.glob("*.tar.gz"))),
            ):
                environment = external / f"venv-{suffix}"
                subprocess.run(
                    ["uv", "venv", str(environment)],
                    cwd=external,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                python = environment / "bin/python"
                subprocess.run(
                    [
                        "uv",
                        "pip",
                        "install",
                        "--offline",
                        "--python",
                        str(python),
                        str(distribution),
                    ],
                    cwd=external,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                clean_env = os.environ.copy()
                clean_env.pop("PYTHONPATH", None)
                clean_env["PYTHONNOUSERSITE"] = "1"
                schemas = subprocess.run(
                    [
                        str(python),
                        "-c",
                        (
                            "from importlib.resources import files; import hashlib, json; "
                            "root=files('collector_engineering').joinpath('schemas'); "
                            "print(json.dumps({name: hashlib.sha256(root.joinpath(name).read_bytes()).hexdigest() "
                            "for name in ('CapturePlaybook.schema.json','PlaybookProposal.schema.json',"
                            "'ScoutSessionTranscript.schema.json')}))"
                        ),
                    ],
                    cwd=external,
                    env=clean_env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                import json

                self.assertEqual(expected, json.loads(schemas.stdout))
                draft_path = external / f"candidate-{suffix}.yaml"
                for command in (
                    ["draft", str(transcript_path), "--output", str(draft_path)],
                    ["validate", str(draft_path)],
                    [
                        "replay",
                        str(draft_path),
                        "--fixtures",
                        str(fixture_copy),
                        "--report",
                        str(external / f"replay-{suffix}.json"),
                    ],
                ):
                    subprocess.run(
                        [str(python), "-m", "collector_engineering", *command],
                        cwd=external,
                        env=clean_env,
                        check=True,
                        capture_output=True,
                        text=True,
                    )


if __name__ == "__main__":
    unittest.main()
