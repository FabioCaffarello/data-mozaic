import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scout.tool_host.protected_replay import DockerProtectedReplayExecutor
from scout.tool_host.protected_run_registry import ProtectedRunRegistry


class ProtectedRunRegistryTests(unittest.TestCase):
    def test_ci_worker_configuration_has_no_bind_or_runtime_controls(self):
        run_id = "c" * 32
        volume = "dm-bc15-" + run_id
        image_id = "sha256:" + ("d" * 64)
        command = DockerProtectedReplayExecutor._worker_args(volume, image_id)
        self.assertIn("--network", command)
        self.assertEqual("none", command[command.index("--network") + 1])
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--security-opt=no-new-privileges:true", command)
        self.assertIn("--pids-limit=32", command)
        self.assertIn("--memory=256m", command)
        self.assertIn("--cpus=0.5", command)
        self.assertIn("--user=65532:65532", command)
        self.assertIn(f"type=volume,source={volume},target=/results", command)
        self.assertFalse(any("type=bind" in item for item in command))
        self.assertFalse(any("docker.sock" in item for item in command))
        self.assertEqual(image_id, command[-1])
        with self.assertRaisesRegex(ValueError, "runtime reference"):
            DockerProtectedReplayExecutor._worker_args("arbitrary-volume", image_id)

    def test_registry_survives_reopen_and_marks_interrupted_run_incomplete(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "runs.sqlite3"
            first = ProtectedRunRegistry(path)
            first.create("a" * 32, "dm-bc15-" + "a" * 32, "session", "principal")
            first.update("a" * 32, "running")

            reopened = ProtectedRunRegistry(path)
            run = reopened.get("a" * 32)
            self.assertEqual("running", run["state"])
            self.assertEqual("session", run["session_id"])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

            executor = object.__new__(DockerProtectedReplayExecutor)
            executor._registry = reopened
            executor._context = "test"
            executor._run = lambda args, **kwargs: subprocess.CompletedProcess(
                args, 0, b"", b""
            )
            executor._expire_runs = lambda: None
            executor._reconcile_runs()
            self.assertEqual("incomplete", reopened.get("a" * 32)["state"])

    def test_single_use_approval_claim_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "private" / "runs.sqlite3"
            ProtectedRunRegistry(path).claim_approval_once(
                "principal", "source", "revision", "2030-01-01T00:00:00+00:00"
            )
            reopened = ProtectedRunRegistry(path)
            with self.assertRaisesRegex(ValueError, "already consumed"):
                reopened.claim_approval_once(
                    "principal", "source", "revision", "2030-01-01T00:00:00+00:00"
                )

    def test_restart_reconciles_orphan_and_expiry_cleanup_is_owned_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            registry = ProtectedRunRegistry(Path(temporary) / "runs.sqlite3")
            executor = object.__new__(DockerProtectedReplayExecutor)
            executor._registry = registry
            executor._context = "test"
            run_id = "b" * 32
            volume = "dm-bc15-" + run_id
            calls = []

            def docker(args, **kwargs):
                calls.append(args)
                if args[2:5] == ["volume", "ls", "--quiet"]:
                    return subprocess.CompletedProcess(
                        args, 0, (volume + "\n").encode(), b""
                    )
                if args[2:4] == ["volume", "inspect"]:
                    fmt = args[args.index("--format") + 1]
                    rendered = (
                        f"{volume} {run_id} bc15"
                        if ".Name" in fmt
                        else f"{run_id} bc15"
                    )
                    return subprocess.CompletedProcess(args, 0, rendered.encode(), b"")
                if args[2:4] == ["volume", "rm"]:
                    return subprocess.CompletedProcess(args, 0, volume.encode(), b"")
                raise AssertionError(args)

            executor._run = docker
            executor._docker_args = lambda *args: ["--context", "test", *args]
            executor._reconcile_runs()
            recovered = registry.get(run_id)
            self.assertEqual("incomplete", recovered["state"])
            self.assertEqual("orphan", recovered["session_id"])

            with registry._connect() as db:
                db.execute(
                    "UPDATE protected_runs SET expires_at=? WHERE run_id=?",
                    ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), run_id),
                )
            executor._expire_runs()
            self.assertEqual("expired", registry.get(run_id)["state"])
            remove_count = sum(call[2:4] == ["volume", "rm"] for call in calls)
            executor._expire_runs()
            self.assertEqual(
                remove_count,
                sum(call[2:4] == ["volume", "rm"] for call in calls),
            )


if __name__ == "__main__":
    unittest.main()
