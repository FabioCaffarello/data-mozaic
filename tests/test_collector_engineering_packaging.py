"""Wheel and sdist are usable from outside the monorepo checkout."""

import hashlib
import importlib.metadata
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "apps/collector-engineering"
FIXTURE = ROOT / "tests/fixtures/collector-engineering/multipage"


def copy_runtime_dependencies(destination: Path) -> None:
    """Copy locked runtime dependencies without checkout or registry paths."""
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text())
    pending = list(project["project"]["dependencies"])
    copied = set()
    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        distribution = importlib.metadata.distribution(requirement.name)
        normalized_name = distribution.metadata["Name"].lower().replace("_", "-")
        if normalized_name in copied:
            continue
        copied.add(normalized_name)
        for dependency in distribution.requires or ():
            child = Requirement(dependency)
            if child.marker is None or child.marker.evaluate({"extra": ""}):
                pending.append(child.name)
        for entry in distribution.files or ():
            relative = Path(str(entry))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            source = Path(distribution.locate_file(entry))
            if source.is_file():
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


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
            "ScoutSessionTranscript.v1.schema.json": hashlib.sha256(
                (
                    ROOT
                    / "contracts/scout/transcript/draft/v1/ScoutSessionTranscript.schema.json"
                ).read_bytes()
            ).hexdigest(),
            "ScoutSessionTranscript.v2.schema.json": hashlib.sha256(
                (
                    ROOT
                    / "contracts/scout/transcript/draft/v2/ScoutSessionTranscript.schema.json"
                ).read_bytes()
            ).hexdigest(),
        }
        with tempfile.TemporaryDirectory(
            prefix="bc15 isolated with spaces "
        ) as temporary:
            external = Path(temporary)
            distributions = external / "dist"
            subprocess.run(
                [
                    "uv",
                    "build",
                    "--offline",
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
            runtime_dependencies = external / "runtime-dependencies"
            copy_runtime_dependencies(runtime_dependencies)

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
                installed = subprocess.run(
                    [
                        "uv",
                        "pip",
                        "install",
                        "--offline",
                        "--no-deps",
                        "--python",
                        str(python),
                        str(distribution),
                    ],
                    cwd=external,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if installed.returncode:
                    self.fail(
                        f"could not install {suffix} into the isolated environment:\n"
                        f"{installed.stdout}\n{installed.stderr}"
                    )
                clean_env = os.environ.copy()
                clean_env.pop("PYTHONPATH", None)
                clean_env["PYTHONNOUSERSITE"] = "1"
                schemas = subprocess.run(
                    [
                        str(python),
                        "-c",
                        (
                            "import sys; from pathlib import Path; "
                            f"sys.path.insert(0, {str(runtime_dependencies)!r}); "
                            "import collector_engineering; "
                            "assert Path(collector_engineering.__file__).is_relative_to("
                            "Path(__import__('sysconfig').get_paths()['purelib'])); "
                            "from importlib.resources import files; import hashlib, json; "
                            "root=files('collector_engineering').joinpath('schemas'); "
                            "print(json.dumps({name: hashlib.sha256(root.joinpath(name).read_bytes()).hexdigest() "
                            "for name in ('CapturePlaybook.schema.json','PlaybookProposal.schema.json',"
                            "'ScoutSessionTranscript.v1.schema.json','ScoutSessionTranscript.v2.schema.json')}))"
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
                ):
                    subprocess.run(
                        [
                            str(python),
                            "-c",
                            (
                                "import runpy,sys; "
                                f"sys.path.insert(0, {str(runtime_dependencies)!r}); "
                                "runpy.run_module('collector_engineering',run_name='__main__')"
                            ),
                            *command,
                        ],
                        cwd=external,
                        env=clean_env,
                        check=True,
                        capture_output=True,
                        text=True,
                    )

                for command in (
                    ["replay", str(draft_path)],
                    ["accept", str(draft_path), str(transcript_path)],
                ):
                    rejected = subprocess.run(
                        [
                            str(python),
                            "-c",
                            (
                                "import runpy,sys; "
                                f"sys.path.insert(0, {str(runtime_dependencies)!r}); "
                                "runpy.run_module('collector_engineering',run_name='__main__')"
                            ),
                            *command,
                        ],
                        cwd=external,
                        env=clean_env,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(2, rejected.returncode, rejected.stderr)
                    self.assertIn("invalid choice", rejected.stderr)
                self.assertFalse((external / f"raw-{suffix}").exists())
                self.assertFalse((external / f"acceptance-raw-{suffix}").exists())


if __name__ == "__main__":
    unittest.main()
