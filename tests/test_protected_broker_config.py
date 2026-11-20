import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scout.tool_host.composition import _private_policy_snapshot
from scout.tool_host.protected_replay import (
    DockerProtectedReplayExecutor,
    ProtectedReplayError,
)


class ProtectedBrokerConfigTests(unittest.TestCase):
    def test_policy_is_snapshotted_outside_workspace_as_owner_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            policy = workspace / "authoring-policy.json"
            original = {"version": 1, "sources": [], "approvals": []}
            policy.write_text(json.dumps(original), encoding="utf-8")
            policy.chmod(0o600)
            private = root / "home" / ".local/state/data-mozaic/scout-bc15/policy.json"

            snapshot = _private_policy_snapshot(policy, private)
            policy.write_text('{"version":1,"sources":["changed"]}', encoding="utf-8")

            self.assertEqual(original, json.loads(snapshot.read_text(encoding="utf-8")))
            self.assertEqual(0o400, stat.S_IMODE(snapshot.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(snapshot.parent.stat().st_mode))
            self.assertFalse(snapshot.is_relative_to(workspace))

    def test_policy_snapshot_rejects_group_readable_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "policy.json"
            source.write_text('{"version":1}', encoding="utf-8")
            source.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "owner-only"):
                _private_policy_snapshot(source, root / "private/policy.json")

    def test_policy_snapshot_rejects_symlink_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual.json"
            actual.write_text('{"version":1}', encoding="utf-8")
            actual.chmod(0o600)
            source = root / "policy.json"
            source.symlink_to(actual)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                _private_policy_snapshot(source, root / "private/policy.json")

    def test_docker_executable_must_be_absolute_and_protected(self):
        with self.assertRaisesRegex(ProtectedReplayError, "absolute path"):
            DockerProtectedReplayExecutor._validate_docker_executable("docker")
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "docker"
            candidate.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            candidate.chmod(0o755)
            with self.assertRaisesRegex(ProtectedReplayError, "trusted-owned"):
                DockerProtectedReplayExecutor._validate_docker_executable(
                    str(candidate)
                )
            self.assertEqual(
                str(candidate.resolve()),
                DockerProtectedReplayExecutor._validate_docker_executable(
                    str(candidate), allow_unprivileged_test_cli=True
                ),
            )

    def test_docker_invocation_ignores_path_shadowing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            shadow = directory / "docker"
            shadow.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            shadow.chmod(0o755)
            executor = object.__new__(DockerProtectedReplayExecutor)
            executor._docker = "/usr/bin/true"
            with (
                patch.dict(os.environ, {"PATH": str(directory)}),
                patch("scout.tool_host.protected_replay.subprocess.run") as run,
            ):
                run.return_value = CompletedProcess(["/usr/bin/true"], 0, b"", b"")
                executor._run(["version"])
            command = run.call_args.args[0]
            environment = run.call_args.kwargs["env"]
            self.assertEqual("/usr/bin/true", command[0])
            self.assertEqual("/usr/bin:/bin", environment["PATH"])
            self.assertNotIn(str(directory), environment["PATH"])


if __name__ == "__main__":
    unittest.main()
