from __future__ import annotations

import hashlib
import json
import os
import plistlib
import stat
import unittest
from pathlib import Path
from types import SimpleNamespace

from scout.adapters.local_custody import LocalFileEvidenceCustody
from scout.domain import IntegrityError, SourceRef
from scout.tool_host.composition import (
    _trusted_config_file,
    _validate_trusted_file_path,
)
from scout.tool_host.server import PROTECTED_AUTHORING_TOOL_NAMES

from tooling.package_scout_broker import (
    BROKER_WORKSPACE_PACKAGES,
    DEPLOYMENT_FILES,
    source_digest,
)
from tooling.verify_scout_bundle import _source_digest

ROOT = Path(__file__).resolve().parents[1]


class ScoutDeploymentArtifactTests(unittest.TestCase):
    def test_bundle_contains_all_internal_runtime_dependencies_and_templates(self):
        self.assertEqual(
            {
                "mozaic-observability",
                "mozaic-bot-build",
                "mozaic-contract-resolution",
                "mozaic-collector-engineering",
                "mozaic-execution-coordination",
                "mozaic-acquisition-lifecycle",
                "mozaic-scout",
            },
            set(BROKER_WORKSPACE_PACKAGES),
        )
        for relative in DEPLOYMENT_FILES:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())
        self.assertTrue((ROOT / "contracts/payload/draft/v1/registry.json").is_file())
        self.assertEqual(source_digest(), _source_digest(ROOT))

    def test_installer_copies_verified_contracts_and_operator_templates(self):
        installer = (ROOT / "tooling/install_scout_bundle.sh").read_text()
        self.assertIn("for resource in contracts config deployments; do", installer)
        self.assertIn('cp -R "$bundle/$resource" "$prefix/$resource"', installer)
        self.assertIn("administrator", installer)

    def test_systemd_template_uses_fixed_installed_runtime_and_hardening(self):
        unit = (
            ROOT / "deployments/systemd/data-mozaic-scout-broker.service"
        ).read_text()
        for required in (
            "User=scout-broker",
            "Group=scout-broker",
            "SupplementaryGroups=docker",
            "WorkingDirectory=/",
            "EnvironmentFile=/etc/data-mozaic/scout/broker.env",
            "current/venv/bin/python -I -m scout.tool_host",
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            "PrivateDevices=yes",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "WantedBy=multi-user.target",
        ):
            with self.subTest(required=required):
                self.assertIn(required, unit)
        self.assertNotIn("--privileged", unit)
        self.assertNotIn("/Volumes/", unit)

    def test_mac_tunnel_template_is_loopback_only_and_parses(self):
        plist_path = (
            ROOT / "deployments/macos/org.datamozaic.scout-tunnel.plist.example"
        )
        document = plistlib.loads(plist_path.read_bytes())
        arguments = document["ProgramArguments"]
        self.assertEqual("scout-tunnel", document["UserName"])
        ssh_config = (ROOT / "config/scout/scout-tunnel-ssh-config.example").read_text()
        self.assertIn("scout-protected-vm", arguments)
        self.assertIn("LocalForward 127.0.0.1:8765 127.0.0.1:8765", ssh_config)
        self.assertIn("StrictHostKeyChecking yes", ssh_config)
        self.assertIn("ForwardAgent no", ssh_config)
        self.assertIn("ClearAllForwardings yes", ssh_config)
        key = (ROOT / "config/scout/scout-tunnel-authorized-key.example").read_text()
        self.assertIn('permitopen="127.0.0.1:8765"', key)
        sshd_match = (
            ROOT / "deployments/sshd/scout-tunnel-match.conf.example"
        ).read_text()
        self.assertIn("AllowTcpForwarding local", sshd_match)
        self.assertIn("PermitOpen 127.0.0.1:8765", sshd_match)

    def test_reviewer_example_has_separate_uid_and_exact_source_scope(self):
        example = json.loads(
            (ROOT / "config/scout/reviewer-identities.example.json").read_text()
        )
        self.assertNotEqual(example["agent_uid"], example["reviewers"][0]["uid"])
        self.assertEqual(
            [{"source_id": "e2e-catalog-single", "revision": "single-resource-v1"}],
            example["reviewers"][0]["source_scopes"],
        )

    def test_protected_http_tool_surface_is_exactly_authoring_only(self):
        self.assertEqual(
            {
                "scout_preflight",
                "scout_start",
                "scout_fetch",
                "scout_record_finding",
                "scout_propose",
                "scout_status",
                "scout_export_transcript",
                "scout_proposal_history",
                "scout_validate_proposal",
                "scout_prepare_promotion",
            },
            PROTECTED_AUTHORING_TOOL_NAMES,
        )

    def test_managed_config_accepts_root_readonly_or_private_service_owned_files(self):
        self.assertTrue(
            _trusted_config_file(
                SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o640), 21001
            )
        )
        self.assertTrue(
            _trusted_config_file(
                SimpleNamespace(st_uid=21001, st_mode=stat.S_IFREG | 0o600), 21001
            )
        )
        self.assertFalse(
            _trusted_config_file(
                SimpleNamespace(st_uid=21002, st_mode=stat.S_IFREG | 0o640), 21001
            )
        )
        self.assertFalse(
            _trusted_config_file(
                SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o662), 21001
            )
        )

    def test_trusted_config_path_rejects_symlink_parent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.mkdir()
            alias = root / "alias"
            alias.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "parent cannot be a symlink"):
                _validate_trusted_file_path(alias / "policy.json")

    def test_reviewer_readable_custody_is_group_read_only_and_opt_in(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir(mode=0o700)
            root.chmod(0o2750)
            content = b"controlled discovery evidence"
            digest = hashlib.sha256(content).hexdigest()
            custody = LocalFileEvidenceCustody(root, reviewer_read_gid=os.getegid())
            artifact = custody.store(
                "session-1",
                SourceRef("controlled", "revision-1"),
                content,
                f"artifact-{digest}",
            )
            for directory in (root / "objects", root / "receipts"):
                mode = directory.stat().st_mode
                self.assertEqual(os.getegid(), directory.stat().st_gid)
                self.assertTrue(mode & stat.S_ISGID)
                self.assertEqual(0o750, stat.S_IMODE(mode) & 0o777)
            for path in (
                root / "objects" / digest,
                root / "receipts" / f"artifact-{digest}.json",
            ):
                self.assertEqual(os.getegid(), path.stat().st_gid)
                self.assertEqual(0o640, stat.S_IMODE(path.stat().st_mode))
            self.assertTrue(custody.verify(artifact))
            with self.assertRaisesRegex(IntegrityError, "ownership or permissions"):
                LocalFileEvidenceCustody(
                    root, reviewer_read_gid=os.getegid() + 1
                )._root()


if __name__ == "__main__":
    unittest.main()
