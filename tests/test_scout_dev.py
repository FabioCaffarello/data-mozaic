"""Bootstrap diagnostics and explicit synthetic-source authorization checks."""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from tooling.scout_dev import (
    FIXTURE_ORIGIN,
    FIXTURE_PREFIX,
    FIXTURE_PROXY_PORT,
    LOCAL_PRINCIPAL,
    SINGLE_SOURCE,
    _artifact_home,
    _authorize_single_fixture,
    _codex_exec_command,
    _database_check,
    _diagnostics,
    _fixture_stack_check,
    _legacy_artifact_home,
    _looks_like_direct_network_command,
    _migrate_managed_proxy_url,
    _prepare_local_files,
    _read_env,
    _source_authorized,
    find_repository_root,
)


class ScoutDevBootstrapTests(unittest.TestCase):
    @staticmethod
    def _isolated_git_environment() -> dict[str, str]:
        environment = os.environ.copy()
        inherited_git_state = {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_PREFIX",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_COUNT",
        }
        for key in tuple(environment):
            if key in inherited_git_state or key.startswith(
                ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
            ):
                environment.pop(key)
        return environment

    def test_network_command_guard_allows_url_parsing_but_rejects_clients(self):
        self.assertFalse(
            _looks_like_direct_network_command(
                "python3 -c 'import urllib.parse; print(urllib.parse.urlparse(x))'"
            )
        )
        for command in (
            "python3 -c 'from urllib.request import urlopen'",
            "python3 -c 'urlopen(url)'",
            "python3 -c 'import requests; requests.get(url)'",
            "curl https://example.test",
        ):
            with self.subTest(command=command):
                self.assertTrue(_looks_like_direct_network_command(command))

    def test_codex_exec_command_uses_cli_compatible_workspace_write_approval(self):
        command = _codex_exec_command(
            Path("/checkout with spaces"),
            Path("/private artifacts"),
            Path("/private artifacts/final.md"),
            "Use the skill.",
        )
        self.assertIn("--approve-for-me", command)
        self.assertEqual(
            ["--sandbox", "workspace-write"],
            command[command.index("--sandbox") : command.index("--sandbox") + 2],
        )
        self.assertEqual(
            ["--cd", "/checkout with spaces"],
            command[command.index("--cd") : command.index("--cd") + 2],
        )

    def test_fixture_proxy_migration_updates_only_generated_local_configuration(self):
        with tempfile.TemporaryDirectory(prefix="scout proxy setup ") as temporary:
            root = Path(temporary) / "checkout"
            local = root / ".local"
            local.mkdir(parents=True, mode=0o700)
            runtime = Path(temporary) / "runtime"
            runtime.mkdir(mode=0o700)
            for name in ("evidence", "artifacts", "certs"):
                (runtime / name).mkdir(mode=0o700)
            policy = local / "authoring-policy.json"
            policy.write_text("{}\n")
            env_path = local / "scout-mcp.env"
            env_path.write_text(
                f"SCOUT_LOCAL_PRINCIPAL={LOCAL_PRINCIPAL}\n"
                f'SCOUT_HOST_POLICY_FILE="{policy}"\n'
                f'SCOUT_EVIDENCE_ROOT="{runtime / "evidence"}"\n'
                f'SCOUT_ARTIFACT_ROOT="{runtime / "artifacts"}"\n'
                'SCOUT_EGRESS_PROXY="http://127.0.0.1:53001"\n'
                f'SSL_CERT_FILE="{runtime / "certs/ca.crt"}"\n'
            )
            env_path.chmod(0o600)
            _migrate_managed_proxy_url(root, runtime, 53001, FIXTURE_PROXY_PORT)
            migrated = env_path.read_text()
            self.assertIn('SCOUT_EGRESS_PROXY="http://127.0.0.1:3128"', migrated)
            self.assertEqual(0o600, stat.S_IMODE(env_path.stat().st_mode))

            env_path.write_text(migrated.replace(LOCAL_PRINCIPAL, "custom-principal"))
            env_path.chmod(0o600)
            before = env_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "not the generated fixture"):
                _migrate_managed_proxy_url(root, runtime, 3128, 3130)
            self.assertEqual(before, env_path.read_bytes())

    def test_fixture_status_rejects_transport_proxy_port_drift(self):
        with mock.patch(
            "tooling.scout_dev._load_runtime",
            return_value=(
                Path("/private/runtime"),
                {"complete": True, "proxy_port": 53001},
            ),
        ):
            self.assertEqual(
                (
                    False,
                    "Scout's governed HTTP transport requires loopback Squid on port 3128",
                ),
                _fixture_stack_check(Path("/repo")),
            )

    def make_checkout(self, root: Path) -> Path:
        root.mkdir(parents=True)
        environment = self._isolated_git_environment()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=environment)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "--allow-empty",
                "-qm",
                "initial",
            ],
            cwd=root,
            check=True,
            env=environment,
        )
        subprocess.run(
            ["git", "checkout", "--detach", "-q", "HEAD"],
            cwd=root,
            check=True,
            env=environment,
        )
        return root

    def test_worktree_root_discovery_handles_detached_checkout_and_spaces(self):
        with tempfile.TemporaryDirectory(
            prefix="scout checkout with spaces "
        ) as temporary:
            root = self.make_checkout(Path(temporary) / "repo with spaces")
            nested = root / "apps" / "scout source"
            nested.mkdir(parents=True)
            with mock.patch.dict(
                os.environ, self._isolated_git_environment(), clear=True
            ):
                self.assertEqual(root.resolve(), find_repository_root(nested))

    def test_local_template_setup_is_private_idempotent_and_does_not_authorize(self):
        with tempfile.TemporaryDirectory(prefix="scout local setup ") as temporary:
            root = Path(temporary) / "checkout with spaces"
            (root / "config/scout").mkdir(parents=True)
            local = root / ".local"
            local.mkdir(mode=0o755)
            sentinel = local / "preexisting-cache-marker"
            sentinel.write_text("preserve local files")
            local.chmod(0o755)
            (root / "config/scout/authoring-policy.example.json").write_text(
                '{"version":1,"sources":[],"approvals":[]}\n'
            )
            (root / "config/scout/authoring-mcp.env.example").write_text(
                "SCOUT_LOCAL_PRINCIPAL=replace\n"
                'SCOUT_HOST_POLICY_FILE=".local/authoring-policy.json"\n'
                "SCOUT_EVIDENCE_ROOT=/tmp/evidence\n"
                "SCOUT_ARTIFACT_ROOT=/tmp/artifacts\n"
                "SCOUT_EGRESS_PROXY=http://127.0.0.1:3137\n"
            )
            (root / ".env.example").write_text(
                "SCOUT_DATABASE_URL=postgresql://localhost/test\n"
            )
            local_env = root / ".env"
            local_env.write_text("SCOUT_DATABASE_URL=existing local value\n")
            local_env.chmod(0o644)
            runtime = Path(temporary) / "private runtime"
            runtime.mkdir(mode=0o700)
            (runtime / "certs").mkdir(mode=0o700)
            (runtime / "certs/ca.crt").write_text("synthetic ca placeholder")

            with mock.patch(
                "tooling.scout_dev.tempfile.gettempdir", return_value=temporary
            ):
                _prepare_local_files(root, runtime, "http://127.0.0.1:3137")
            policy_path = root / ".local/authoring-policy.json"
            mcp_env_path = root / ".local/scout-mcp.env"
            original_policy = policy_path.read_bytes()
            original_env = mcp_env_path.read_bytes()
            with mock.patch(
                "tooling.scout_dev.tempfile.gettempdir", return_value=temporary
            ):
                artifact_root = _artifact_home(root)
                legacy_artifact_root = _legacy_artifact_home(root)
                canonical_temp = Path(temporary).resolve()
            self.assertTrue(artifact_root.is_relative_to(canonical_temp))
            self.assertEqual(
                str(artifact_root),
                _read_env(mcp_env_path)["SCOUT_ARTIFACT_ROOT"],
            )
            (runtime / "artifacts").mkdir(mode=0o700)
            mcp_env_path.write_bytes(
                original_env.replace(
                    str(artifact_root).encode(), str(runtime / "artifacts").encode()
                )
            )
            mcp_env_path.chmod(0o600)
            with mock.patch(
                "tooling.scout_dev.tempfile.gettempdir", return_value=temporary
            ):
                _prepare_local_files(root, runtime, "http://127.0.0.1:3137")
                migrated_env = mcp_env_path.read_bytes()
                mcp_env_path.write_bytes(
                    migrated_env.replace(
                        str(artifact_root).encode(),
                        str(legacy_artifact_root).encode(),
                    )
                )
                mcp_env_path.chmod(0o600)
                _prepare_local_files(root, runtime, "http://127.0.0.1:3137")

            self.assertEqual(original_policy, policy_path.read_bytes())
            self.assertEqual(original_env, mcp_env_path.read_bytes())
            self.assertEqual([], json.loads(original_policy.decode())["sources"])
            self.assertEqual([], json.loads(original_policy.decode())["approvals"])
            self.assertEqual(0o700, stat.S_IMODE((root / ".local").stat().st_mode))
            self.assertEqual("preserve local files", sentinel.read_text())
            self.assertEqual(0o600, stat.S_IMODE(mcp_env_path.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE((root / ".env").stat().st_mode))
            self.assertEqual(
                "SCOUT_DATABASE_URL=existing local value\n", local_env.read_text()
            )

    def test_missing_policy_and_unapproved_source_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertFalse(_source_authorized(root, LOCAL_PRINCIPAL, *SINGLE_SOURCE))
            local = root / ".local"
            local.mkdir(mode=0o700)
            policy = local / "authoring-policy.json"
            policy.write_text('{"version":1,"sources":[],"approvals":[]}\n')
            policy.chmod(0o600)
            self.assertFalse(_source_authorized(root, LOCAL_PRINCIPAL, *SINGLE_SOURCE))

    def test_explicit_test_authorization_is_narrow_and_expires(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / ".local"
            local.mkdir(mode=0o700)
            policy = local / "authoring-policy.json"
            policy.write_text('{"version":1,"sources":[],"approvals":[]}\n')
            policy.chmod(0o600)
            _authorize_single_fixture(root, LOCAL_PRINCIPAL)
            self.assertTrue(_source_authorized(root, LOCAL_PRINCIPAL, *SINGLE_SOURCE))
            value = json.loads(policy.read_text())
            self.assertEqual(1, len(value["sources"]))
            self.assertEqual(1, len(value["approvals"]))
            self.assertEqual(
                [{"origin": FIXTURE_ORIGIN, "path_prefixes": [FIXTURE_PREFIX]}],
                value["sources"][0]["grants"],
            )
            self.assertEqual(1, value["sources"][0]["limits"]["max_requests"])
            self.assertEqual(120, value["sources"][0]["limits"]["max_duration_seconds"])
            self.assertEqual(1, value["approvals"][0]["limits"]["max_requests"])
            expires_at = datetime.fromisoformat(value["approvals"][0]["expires_at"])
            self.assertLessEqual(expires_at - datetime.now(UTC), timedelta(minutes=30))
            self.assertEqual(0o600, stat.S_IMODE(policy.stat().st_mode))
            with self.assertRaisesRegex(RuntimeError, "already active"):
                _authorize_single_fixture(root, LOCAL_PRINCIPAL)
            self.assertEqual(1, len(json.loads(policy.read_text())["approvals"]))

    def test_database_and_fixture_preflight_report_missing_prerequisites(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual((False, "local .env missing"), _database_check(root))
            self.assertEqual(
                (False, "Squid, DNS control or fixture certificate is unavailable"),
                _fixture_stack_check(root),
            )

    def test_status_reports_checkout_dependency_docker_database_policy_and_mcp_gaps(
        self,
    ):
        with tempfile.TemporaryDirectory(
            prefix="scout status with spaces "
        ) as temporary:
            root = self.make_checkout(Path(temporary) / "repo with spaces")
            with (
                mock.patch("tooling.scout_dev.shutil.which", return_value=None),
                mock.patch(
                    "tooling.scout_dev._database_check",
                    return_value=(False, "PostgreSQL unavailable"),
                ),
                mock.patch(
                    "tooling.scout_dev._fixture_stack_check",
                    return_value=(False, "DNS unavailable"),
                ),
                mock.patch(
                    "tooling.scout_dev._wrapper_smoke",
                    return_value=(False, "dependencies missing"),
                ),
            ):
                report = _diagnostics(root, *SINGLE_SOURCE)
            self.assertFalse(report["ready"])
            for key in (
                "dependencies",
                "docker",
                "postgres_migrations",
                "https_squid_dns",
                "local_policy_file",
                "source_approval",
                "mcp_launcher_config",
                "bc15_external_cwd",
                "codex_cli",
                "codex_authenticated",
                "codex_mcp_registered",
            ):
                self.assertFalse(report["checks"][key], key)


if __name__ == "__main__":
    unittest.main()
